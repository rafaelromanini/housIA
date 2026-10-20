"""Smart App "Sugestão de perguntas": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="perguntas",
    nome="Sugestão de perguntas",
    aba="vivo",
    ordem=10,
    recursos_fhir=["Condition", "Observation", "MedicationRequest"],
    saida=Saida(card="lista"),
    refs_obrigatorias=False,
    vazio="Nada pendente por enquanto.",
)


def run(ctx: AppContext) -> list[AppItem]:
    sev = {"alta": "critico", "media": "atencao", "baixa": "info"}
    return [
        AppItem(texto=q.pergunta, detalhe=q.motivo, severidade=sev[q.prioridade], refs=q.refs, origem=q.origem)
        for q in ctx.ins.perguntas
    ]
