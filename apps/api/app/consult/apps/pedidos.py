"""Smart App "Pedidos em rascunho": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="pedidos",
    nome="Pedidos em rascunho",
    aba="pedidos",
    ordem=10,
    recursos_fhir=["MedicationRequest", "ServiceRequest"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhum pedido em rascunho.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(
            texto=f"{d.acao.replace('_', ' ')}: {d.item}",
            detalhe=d.justificativa,
            severidade=d.gravidade,
            refs=d.refs,
            origem=d.origem,
        )
        for d in ctx.ins.pedidos
    ]
