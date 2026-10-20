"""Smart App "Alertas de segurança": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="alertas",
    nome="Alertas de segurança",
    aba="vivo",
    ordem=20,
    recursos_fhir=["MedicationRequest", "AllergyIntolerance", "Observation"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhum alerta de segurança.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(texto=a.texto, severidade=a.severidade, refs=a.refs, detalhe=a.categoria or "") for a in ctx.ins.alertas
    ]
