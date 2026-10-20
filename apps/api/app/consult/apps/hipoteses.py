"""Smart App "Hipóteses diagnósticas": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="hipoteses",
    nome="Hipóteses diagnósticas",
    aba="raciocinio",
    ordem=10,
    recursos_fhir=["Condition", "Observation", "MedicationRequest"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhuma hipótese levantada.",
)


def run(ctx: AppContext) -> list[AppItem]:
    sev = {"alta": "atencao", "media": "info", "baixa": "info"}
    return [
        AppItem(
            texto=f"{h.hipotese} ({h.cid})" if h.cid else h.hipotese,
            detalhe=h.proximos_passos,
            severidade=sev[h.confianca],
            refs=h.refs,
            origem=h.origem,
        )
        for h in ctx.ins.hipoteses
    ]
