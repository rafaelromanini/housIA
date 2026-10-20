"""Smart App "Gestação e lactação": os medicamentos em uso ou citados × a segurança na gestação e na amamentação."""

from app.consult.apps.base import AppContext, BaseCurada, Gatilho, Manifest, Saida, sev_rank
from app.consult.schemas import AppItem
from app.consult.text import keyword_status, norm
from app.reference import _read_csv, drugs, matches

MANIFEST = Manifest(
    id="gestacao_lactacao",
    nome="Gestação e lactação",
    aba="medicamentos",
    ordem=40,
    gatilho=Gatilho(sexo="F", idade_min=10, idade_max=55),
    recursos_fhir=["Condition", "MedicationRequest"],
    base=BaseCurada(
        arquivo="smart_apps_gestacao_lactacao.csv",
        versao="1.0.0",
        fonte="Bula (ANVISA) e LactMed: base de demonstração, não exaustiva",
    ),
    saida=Saida(card="tabela", colunas=["Medicamento", "Gestação", "Lactação", "Conduta"]),
)

PREGNANT = ("gravida", "gestante", "gravidez", "gestacao")
LACTATING = ("amamentando", "amamento", "dar de mamar", "dando de mamar", "lactante", "dando mama", "dou mama")
# "minha filha está grávida" não é a paciente: outra pessoa como sujeito anula a oração
OTHER_SUBJECT = tuple(
    f"{p} esta"
    for p in ("filha", "neta", "esposa", "mulher", "irma", "nora", "amiga", "sobrinha", "vizinha", "prima", "cunhada")
)
# passado, dúvida e exame negativo também não são gestação em curso
NOT_NOW = (
    "ja fui",
    "ja estive",
    "ja tive",
    "sera que",
    "quando estava",
    "quando fiquei",
    "teste de gravidez deu negativo",
    "teste de gravidez negativo",
    "teste de gravidez foi negativo",
    "teste de gravidez nao deu positivo",
)
SEVERITY = {"contraindicado": "critico", "evitar": "atencao", "cautela": "info", "compativel": "ok"}
LABEL = {"contraindicado": "Contraindicado", "evitar": "Evitar", "cautela": "Cautela", "compativel": "Compatível"}


def _state(ctx: AppContext, icd_prefixes: tuple[str, ...], words: tuple[str, ...]) -> tuple[bool, str | None]:
    """(sim ou não, ref de onde veio): diagnóstico ativo no prontuário ou fala afirmativa de paciente/acompanhante."""
    for icd, _, cond in ctx.chart.conditions:
        if icd.startswith(icd_prefixes):
            return True, f"Condition/{cond['id']}"
    for s in ctx.segments:
        if (
            s.falante in ("paciente", "acompanhante")
            and keyword_status(norm(s.texto), words, skip_questions=True, excludes=OTHER_SUBJECT + NOT_NOW)
            == "presente"
        ):
            return True, f"Fala/{s.n}"
    return False, None


def run(ctx: AppContext) -> list[AppItem] | None:
    pregnant, p_ref = _state(ctx, ("O", "Z32.1", "Z33", "Z34", "Z35"), PREGNANT)
    lactating, l_ref = _state(ctx, ("Z39.1",), LACTATING)
    if not (pregnant or lactating):
        return None
    catalog = drugs()
    in_use: list[tuple[str, str, str, str]] = []  # (princípio, classe, nome, ref)
    for m in ctx.record.active_meds:
        in_use.append((m.ingredient, m.drug_class, m.name, m.ref))
    seen = {u[0] for u in in_use}
    for m in ctx.ins.medicamentos:  # ditos ou planejados na conversa
        text = norm(m.medicamento)
        for d in catalog.values():
            if d.ingredient not in seen and d.ingredient in text:
                seen.add(d.ingredient)
                in_use.append((d.ingredient, d.drug_class, d.name, m.refs[0] if m.refs else ctx.patient_ref))
    estado = " e ".join(x for x, on in (("gestação", pregnant), ("lactação", lactating)) if on)
    items = [
        AppItem(
            texto=f"Paciente em {estado}",
            detalhe="Origem: prontuário ou fala da paciente. Confirme com ela.",
            severidade="info",
            refs=[r for r in (p_ref, l_ref) if r],
            celulas=[f"Paciente em {estado}", "—", "—", "Confirme com a paciente."],
        )
    ]
    for row in _read_csv("smart_apps_gestacao_lactacao.csv"):
        for ingredient, klass, name, ref in in_use:
            if not matches(row["alvo"], ingredient, klass):
                continue
            worst = []
            if pregnant:
                worst.append(row["gestacao"])
            if lactating:
                worst.append(row["lactacao"])
            level = min(worst, key=lambda x: sev_rank(SEVERITY[x]))
            items.append(
                AppItem(
                    texto=f"{name}: {row['orientacao']}",
                    detalhe=f"Fonte: {row['fonte']}",
                    severidade=SEVERITY[level],  # type: ignore[arg-type]
                    refs=[ref],
                    celulas=[name, LABEL[row["gestacao"]], LABEL[row["lactacao"]], row["orientacao"]],
                )
            )
    return items
