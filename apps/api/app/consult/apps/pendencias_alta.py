"""Smart App "Pendências para a alta": o que ainda falta resolver para este paciente internado ir para casa."""

from app.consult.apps.base import AppContext, Gatilho, Manifest, Saida
from app.consult.chart import ago
from app.consult.schemas import AppItem

MANIFEST = Manifest(
    id="pendencias_alta",
    nome="Pendências para a alta",
    aba="transicao",
    ordem=10,
    gatilho=Gatilho(tipos=["visita"], internado=True),
    recursos_fhir=["Encounter", "ServiceRequest", "MedicationRequest", "RelatedPerson"],
    saida=Saida(card="lista"),
    vazio="Nenhuma pendência para a alta no prontuário.",
)


def run(ctx: AppContext) -> list[AppItem]:
    r = ctx.record
    hosp = r.hospitalization
    enc = f"Encounter/{hosp['id']}" if hosp else ctx.patient_ref
    items = [
        AppItem(
            texto=f"Exame sem resultado: {e.texto}",
            detalhe=f"Pedido {ago(e.pedido_em, ctx.now)}" if e.pedido_em else "Sem data do pedido",
            severidade="atencao",
            refs=[e.ref],
        )
        for e in r.pending_exams
    ]
    if not ctx.extra.get("medrec_runs"):
        items.append(
            AppItem(
                texto="Reconciliação medicamentosa da alta ainda não feita",
                detalhe="Compare os medicamentos de casa com a prescrição da internação antes de dar alta.",
                severidade="atencao",
                refs=[enc],
            )
        )
    if r.caregiver is None:
        items.append(
            AppItem(
                texto="Sem cuidador registrado para os primeiros dias em casa",
                detalhe="Confirme com o paciente e a família quem vai acompanhar a volta para casa.",
                severidade="atencao",
                refs=[ctx.patient_ref],
            )
        )
    for m in r.proposed_meds:
        items.append(
            AppItem(
                texto=f"Medicamento novo a orientar: {m.name}",
                detalhe=" ".join(x for x in (m.dose, m.route) if x) or "Rascunho de prescrição ainda não emitido.",
                refs=[m.ref],
            )
        )
    for a in ctx.ins.alertas:
        if a.severidade == "critico":
            items.append(AppItem(texto=a.texto, detalhe="Alerta crítico aberto", severidade="critico", refs=a.refs))
    return items
