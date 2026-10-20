"""Smart App "Cirurgia": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="cirurgia",
    nome="Cirurgia",
    aba="raciocinio",
    ordem=30,
    recursos_fhir=["Procedure", "Condition"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Sem dado cirúrgico.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [AppItem(texto=c.texto, detalhe=c.tipo, refs=c.refs, origem=c.origem) for c in ctx.ins.cirurgia]
