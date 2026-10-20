"""Smart App "Queixa principal": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="queixas",
    nome="Queixa principal",
    aba="vivo",
    ordem=40,
    recursos_fhir=[],
    saida=Saida(card="tags"),
    refs_obrigatorias=False,
    vazio="Nenhuma queixa identificada ainda.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(texto=t.rotulo, detalhe=t.detalhe, tags=[t.status], refs=t.refs, origem=t.origem)
        for t in ctx.ins.queixas
    ]
