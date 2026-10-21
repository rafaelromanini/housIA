"""Smart App "Escalas clínicas" (#27): pontuação determinística, com o ponto a ponto e a fonte de cada escala.

Mostra o que o pacote da especialidade lista (data/reference/specialties/*.yaml) e o que o prontuário pede (CHA2DS2-VASc
em quem tem fibrilação atrial). Respostas dadas pelo profissional valem mais que a estimativa pela fala.
"""

from app.consult import knowledge, scales
from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="escalas",
    nome="Escalas clínicas",
    aba="raciocinio",
    ordem=45,
    recursos_fhir=["Condition", "Observation", "Patient"],
    saida=Saida(card="lista"),
    versao="1.0.0",
    refs_obrigatorias=False,  # resposta dada pelo profissional não tem Fala/N
    vazio="Nenhuma escala se aplica a este atendimento ainda.",
)


def _item(r: scales.Resultado, patient_ref: str) -> AppItem:
    linhas = []
    for p in r.pontos:
        pontos = f" = {p.pontos} ponto(s)" if r.maximo else ""
        refs = f" [{', '.join(p.refs)}]" if p.refs else ""
        linhas.append(f"{p.rotulo}: {p.resposta}{pontos}{refs}")
    if r.faltam:
        linhas.append("Faltam: " + "; ".join(r.faltam))
    if r.estimada:
        linhas.append("Estimada pela fala do paciente: confirme as respostas.")
    if r.aviso:
        linhas.append(r.aviso)
    linhas.append(f"Fonte: {r.fonte}")
    texto = r.resumo() if r.maximo else f"{r.nome}: {r.faixa}"
    return AppItem(texto=texto, detalhe="\n".join(linhas), severidade=r.severidade, refs=r.refs or [patient_ref])  # type: ignore[arg-type]


def avaliar(ctx: AppContext) -> list[scales.Resultado]:
    """Escalas do atendimento: prontuário, pacote da especialidade e respostas enviadas pelo profissional."""
    out: list[scales.Resultado] = []
    wanted = set(knowledge.specialty_pack(ctx.specialty).escalas) if ctx.specialty in knowledge.SPECIALTIES else set()
    given: dict = (ctx.extra or {}).get("escalas") or {}
    r = ctx.record
    conds = ctx.chart.conditions
    if scales.tem_fa(conds):
        out.append(scales.cha2ds2_vasc(r.age, r.sex == "F", conds, ctx.patient_ref))
    if "ipss" in wanted or "ipss" in given:
        answers, refs = scales.ipss_pela_fala(ctx.segments)
        answers = answers | {k: str(v) for k, v in (given.get("ipss") or {}).get("respostas", {}).items()}
        refs = {
            k: v for k, v in refs.items() if k in answers and k not in (given.get("ipss") or {}).get("respostas", {})
        }
        if answers or "ipss" in given:
            out.append(scales.calcular("ipss", answers, refs))
    has_ic = any(icd.startswith(("I50", "I11.0", "I13.0", "I13.2")) for icd, _, _ in conds)
    if "nyha" in wanted or has_ic:
        classe, refs_ = scales.nyha_pela_fala(ctx.segments)
        manual = (given.get("nyha") or {}).get("respostas", {}).get("classe")
        if manual in scales.NYHA_CLASSES:
            out.append(scales.nyha(manual, [], "resposta"))
        elif classe:
            out.append(scales.nyha(classe, refs_))
    for key in ("phq9", "gad7", "morse", "braden", "ivcf20"):
        if key in given:
            out.append(scales.calcular(key, {k: str(v) for k, v in given[key].get("respostas", {}).items()}))
    if "imc" in given:
        resp = given["imc"].get("respostas", {})
        w = ctx.chart.weight()
        if w and resp.get("altura_cm"):
            out.append(scales.imc(w[0], float(resp["altura_cm"]), r.age, w[1]))
    return out


def run(ctx: AppContext) -> list[AppItem]:
    done = avaliar(ctx)
    items = [_item(x, ctx.patient_ref) for x in done]
    wanted = knowledge.specialty_pack(ctx.specialty).escalas if ctx.specialty in knowledge.SPECIALTIES else []
    taken = {x.id for x in done}
    pending = [scales.QUESTIONARIOS[i].nome for i in wanted if i in scales.QUESTIONARIOS and i not in taken]
    if pending:  # as do pacote da especialidade que ainda esperam resposta do profissional
        items.append(
            AppItem(
                texto="Escalas deste perfil ainda sem respostas: " + "; ".join(pending),
                detalhe="Registre as respostas na tela de escalas para ver a pontuação, o ponto a ponto e a fonte.",
                refs=[ctx.patient_ref],
            )
        )
    return items
