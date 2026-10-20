"""Smart App "Histórico médico": migrado para o registro (#21); lê o que o engine já calculou, sem alterar as regras."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="historico",
    nome="Histórico médico",
    aba="prontuario",
    ordem=10,
    recursos_fhir=["Condition", "Procedure", "Encounter"],
    saida=Saida(card="linha_do_tempo"),
    refs_obrigatorias=False,
    vazio="Sem histórico registrado.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(texto=h.texto, quando=h.quando, detalhe=h.tipo.replace("_", " "), refs=h.refs, origem=h.origem)
        for h in ctx.ins.historico
    ]
