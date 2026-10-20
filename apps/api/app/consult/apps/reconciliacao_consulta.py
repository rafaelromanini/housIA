"""Smart App "Reconciliação na consulta": o que o paciente disse tomar × o que o prontuário registra."""

from app.consult.apps.base import AppContext, Manifest, Saida
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="reconciliacao_consulta",
    nome="Reconciliação na consulta",
    aba="medicamentos",
    ordem=30,
    recursos_fhir=["MedicationRequest"],
    saida=Saida(card="tabela", colunas=["Medicamento", "Prontuário", "Paciente disse", "Situação"]),
    vazio="Nenhum medicamento para reconciliar ainda.",
)

SITUACAO = {
    "em_uso": ("Registrado e em uso", "ok"),
    "relatado": ("Dito na conversa, sem registro", "atencao"),
    "suspenso": ("Suspenso", "info"),
    "planejado": ("Planejado nesta consulta", "info"),
    "proposto": ("Proposto, ainda não emitido", "info"),
}


def run(ctx: AppContext) -> list[AppItem]:
    items = []
    for m in ctx.ins.medicamentos:
        label, severity = SITUACAO[m.status]
        in_chart = any(r.startswith("MedicationRequest/") for r in m.refs)
        said = any(r.startswith("Fala/") for r in m.refs)
        items.append(
            AppItem(
                texto=m.medicamento,
                detalhe=m.observacao or "",
                severidade=severity,  # type: ignore[arg-type]
                refs=m.refs,
                celulas=[m.medicamento, m.posologia if in_chart else "—", "Sim" if said else "Não citado", label],
            )
        )
    items.sort(key=lambda i: 0 if i.severidade == "atencao" else 1)
    return items
