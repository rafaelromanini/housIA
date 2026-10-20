"""Smart App "Sumarizador": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="resumo",
    nome="Sumarizador",
    aba="vivo",
    ordem=60,
    recursos_fhir=["Condition", "MedicationRequest", "Observation"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Sem resumo ainda.",
)


def run(ctx: AppContext) -> list[AppItem]:
    if ctx.ins.resumo is None:
        return []
    return [AppItem(texto=f.texto, refs=f.refs, origem=f.origem, severidade="info") for f in ctx.ins.resumo.frases]
