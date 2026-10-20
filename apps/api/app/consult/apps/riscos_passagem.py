"""Smart App "Riscos para a passagem": o que o próximo plantão precisa saber sobre este paciente internado."""

from app.consult.apps.base import AppContext, Gatilho, Manifest, Saida, sev_rank
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="riscos_passagem",
    nome="Riscos para a passagem",
    aba="transicao",
    ordem=20,
    gatilho=Gatilho(tipos=["visita"], internado=True),
    recursos_fhir=["Observation", "AllergyIntolerance", "MedicationRequest", "Encounter"],
    saida=Saida(card="lista"),
    vazio="Nenhum risco especial para a passagem de plantão.",
)

MORSE_ALTO = 45  # Morse: 45 ou mais é alto risco de queda
BRADEN_ALTO = 12  # Braden: 12 ou menos é alto risco de lesão por pressão


def run(ctx: AppContext) -> list[AppItem]:
    c = ctx.chart
    items: list[AppItem] = []
    morse = c.latest_vital("morse", 24 * 14)
    if morse and float(morse.value["valor"]) >= MORSE_ALTO:
        items.append(
            AppItem(
                texto=f"Risco alto de queda (Morse {float(morse.value['valor']):g})",
                detalhe="Manter grades, campainha ao alcance e acompanhante na transferência.",
                severidade="atencao",
                refs=[morse.source_ref],
            )
        )
    braden = c.latest_vital("braden", 24 * 14)
    if braden and float(braden.value["valor"]) <= BRADEN_ALTO:
        items.append(
            AppItem(
                texto=f"Risco alto de lesão por pressão (Braden {float(braden.value['valor']):g})",
                detalhe="Manter mudança de decúbito e superfície de alívio de pressão.",
                severidade="atencao",
                refs=[braden.source_ref],
            )
        )
    for a in ctx.ins.alergias:
        if a.criticidade == "alta":
            items.append(
                AppItem(texto=f"Alergia grave: {a.alergeno}", detalhe=a.reacao, severidade="critico", refs=a.refs)
            )
    for m in ctx.ins.sugestoes_medicamentos:
        if m.tipo == "alto_alerta":
            items.append(
                AppItem(texto=m.texto, detalhe="Medicamento de alto alerta", severidade=m.severidade, refs=m.refs)
            )
    for a in ctx.ins.alertas:
        if a.severidade == "critico":
            items.append(AppItem(texto=a.texto, severidade="critico", refs=a.refs))
    for e in ctx.record.pending_exams:
        if e.proposto or e.imagem:
            items.append(AppItem(texto=f"Exame a acompanhar: {e.texto}", severidade="info", refs=[e.ref]))
    items.sort(key=lambda i: sev_rank(i.severidade))
    return items
