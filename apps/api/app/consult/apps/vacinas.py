"""Smart App "Vacinas": o calendário por idade e sexo × as doses (Immunization) registradas no prontuário."""

from app.consult.apps.base import AppContext, BaseCurada, Gatilho, Manifest, Saida, in_age, sev_rank
from app.consult.chart import fmt_date
from app.consult.schemas import AppItem
from app.consult.text import norm
from app.reference import _read_csv
from app.timeline.record import parse_dt

MANIFEST = Manifest(
    id="vacinas",
    nome="Vacinas",
    aba="prontuario",
    ordem=50,
    gatilho=Gatilho(tipos=["consulta"]),
    recursos_fhir=["Immunization"],
    base=BaseCurada(
        arquivo="smart_apps_vacinas.csv",
        versao="1.0.0",
        fonte="Calendário Nacional de Vacinação (PNI/MS): resumo de demonstração, não exaustivo",
    ),
    saida=Saida(card="tabela", colunas=["Vacina", "Situação", "Última dose", "Orientação"]),
    vazio="Nenhuma vacina do calendário se aplica a esta idade.",
)


def _doses(ctx: AppContext) -> list[tuple[str, object, str]]:
    out = []
    for imm in ctx.record.resources.get("Immunization", []):
        if imm.get("status") == "not-done":
            continue
        code = imm.get("vaccineCode") or {}
        text = norm(" ".join([code.get("text", ""), *(c.get("display", "") for c in code.get("coding", []))]))
        out.append((text, parse_dt(imm.get("occurrenceDateTime")), f"Immunization/{imm['id']}"))
    return out


def run(ctx: AppContext) -> list[AppItem]:
    r = ctx.record
    doses = _doses(ctx)
    items = []
    for row in _read_csv("smart_apps_vacinas.csv"):
        if row["sexo"] and row["sexo"] != r.sex:
            continue
        if not in_age(r.age, row["idade_min"], row["idade_max"]):
            continue
        terms = row["termos"].split("|")
        hits = [d for d in doses if any(t in d[0] for t in terms)]
        months = int(row["intervalo_meses"])
        if not hits:
            status, sev, when, refs = "Sem registro", "info", "—", [ctx.patient_ref]
        else:
            last = max(hits, key=lambda d: d[1].timestamp() if d[1] else 0)
            when = fmt_date(last[1]) or "—"
            vencida = months and last[1] is not None and (ctx.now - last[1]).days > months * 30.4
            status, sev, refs = ("Vencida", "atencao", [last[2]]) if vencida else ("Em dia", "ok", [last[2]])
        items.append(
            AppItem(
                texto=row["vacina"],
                detalhe=f"Fonte: {row['fonte']}",
                severidade=sev,  # type: ignore[arg-type]
                refs=refs,
                celulas=[row["vacina"], status, when, row["orientacao"]],
            )
        )
    items.sort(key=lambda i: sev_rank(i.severidade))
    return items
