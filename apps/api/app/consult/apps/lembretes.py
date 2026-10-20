"""Smart App "Lembretes": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="lembretes",
    nome="Lembretes",
    aba="vivo",
    ordem=30,
    recursos_fhir=["AllergyIntolerance", "Observation", "Encounter"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nenhum lembrete pendente.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [AppItem(texto=r.texto, detalhe=r.fonte, severidade=r.severidade, refs=r.refs) for r in ctx.ins.lembretes]
