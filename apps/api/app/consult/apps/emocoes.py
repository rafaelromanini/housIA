"""Smart App "Análise multidimensional": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="emocoes",
    nome="Análise multidimensional",
    aba="vivo",
    ordem=50,
    recursos_fhir=[],
    saida=Saida(card="linha_do_tempo"),
    refs_obrigatorias=False,
    vazio="Sem emoções identificadas.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [AppItem(texto=e.rotulo, detalhe=e.abordagem, refs=e.refs, origem=e.origem) for e in ctx.ins.emocoes]
