"""Smart App "Histórico de medicamentos": migrado para o registro (#21), lê o que o engine já calculou."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="medicamentos",
    nome="Histórico de medicamentos",
    aba="medicamentos",
    ordem=10,
    recursos_fhir=["MedicationRequest"],
    saida=Saida(card="tabela", colunas=["Medicamento", "Posologia", "Situação", "Desde"]),
    refs_obrigatorias=False,
    vazio="Nenhum medicamento registrado.",
)


def run(ctx: AppContext) -> list[AppItem]:
    return [
        AppItem(
            texto=m.medicamento,
            celulas=[m.medicamento, m.posologia, m.status.replace("_", " "), m.desde or "—"],
            refs=m.refs,
        )
        for m in ctx.ins.medicamentos
    ]
