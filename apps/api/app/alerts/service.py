from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import get_settings
from app.db.models import Alert, JourneyTask

SEVERITY_RANK = {"info": 0, "atencao": 1, "critico": 2}


def same_claim(alert: Alert, title: str, refs: list[str]) -> bool:
    """Mesmo título ou mesmas fontes (Jaccard ≥ 0,5 entre as refs).

    O texto muda entre gerações (horários reancorados a cada carga, outra redação do LLM, mock × real), mas os ids
    dos recursos FHIR citados não: por isso as fontes identificam a afirmação melhor que o título.
    """
    if alert.title == title:
        return True
    a, b = set(alert.source_refs or []), set(refs)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.5


def upsert_alert(
    db: Session,
    *,
    patient_id: str,
    source: str,
    severity: str,
    title: str,
    detail: str = "",
    refs: list[str] | None = None,
    claimed: set[int] | None = None,
) -> Alert:
    """Cria o alerta ou atualiza o equivalente ainda ativo (aberto/reconhecido), sem duplicar a cada regeneração.

    `claimed` guarda os alertas já usados na mesma geração, para dois itens distintos não caírem no mesmo alerta.
    Se a severidade sobe num alerta já reconhecido, ele volta a "aberto" (e isso vai para a auditoria).
    """
    title, refs = title[:300], refs or []
    active = db.scalars(
        select(Alert)
        .where(
            Alert.patient_id == patient_id,
            Alert.source == source,
            Alert.status.in_(("aberto", "reconhecido")),
        )
        .order_by(Alert.id)
    ).all()
    for alert in active:
        if claimed is not None and alert.id in claimed:
            continue
        if same_claim(alert, title, refs):
            escalated = SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(alert.severity, 0)
            alert.title, alert.detail, alert.source_refs, alert.severity = title, detail, refs, severity
            if escalated and alert.status == "reconhecido":
                alert.status, alert.acted_by, alert.acted_at = "aberto", None, None
                log_action(
                    db,
                    None,
                    "alerta.reaberto",
                    "alert",
                    alert.id,
                    {"patient_id": patient_id, "titulo": title, "severidade": severity},
                    commit=False,
                )
            if claimed is not None:
                claimed.add(alert.id)
            return alert
    alert = Alert(
        patient_id=patient_id,
        source=source,
        severity=severity,
        title=title,
        detail=detail,
        source_refs=refs,
        status="aberto",
        created_at=clock.now(),
    )
    db.add(alert)
    db.flush()
    if claimed is not None:
        claimed.add(alert.id)
    return alert


def consult_alert_facts(db: Session, patient_id: str) -> list:
    """Alertas críticos da consulta ainda abertos, como fatos duros da passagem de plantão (precisam de fonte FHIR)."""
    from app.handoff.schemas import Fact

    rows = db.scalars(
        select(Alert)
        .where(
            Alert.patient_id == patient_id,
            Alert.source == "consulta",
            Alert.severity == "critico",
            Alert.status == "aberto",
        )
        .order_by(Alert.id)
    ).all()
    return [
        Fact(
            tipo="alerta_consulta",
            texto=f"{a.title} ({a.detail})",
            severidade="critico",
            refs=list(a.source_refs or []),
        )
        for a in rows
        if a.source_refs
    ]


COMM_SOURCE = "comunicacao"


def communication_facts(db: Session, patient_id: str) -> list:
    """Dúvidas e preocupações do paciente ou da família que ficaram sem resposta na consulta (#43), ainda abertas: vão
    para a passagem de plantão como fato (sinal de comunicação, não diagnóstico), com teach-back recomendado."""
    from app.handoff.schemas import Fact

    rows = db.scalars(
        select(Alert)
        .where(Alert.patient_id == patient_id, Alert.source == COMM_SOURCE, Alert.status.in_(("aberto", "reconhecido")))
        .order_by(Alert.id)
    ).all()
    return [
        Fact(
            tipo="preocupacao_nao_respondida",
            texto=f"{a.title} ({a.detail})",
            severidade="atencao",
            refs=list(a.source_refs),
        )
        for a in rows
        if a.source_refs
    ]


def tarefa_facts(db: Session, patient_id: str) -> list:
    """Tarefas abertas do paciente (pendências da consulta e da jornada, #47) como fatos da passagem de plantão: com
    responsável, prazo e a fonte (a tarefa FHIR e o encontro). Tarefa crítica vai aos dois públicos; vencida sobe para
    atenção. O motivo (consulta e fala) fica no texto: a fala em si nunca vai."""
    from app.handoff.schemas import Fact
    from app.journey.reference import PAPEL_LABEL

    now = clock.now()
    rows = db.scalars(
        select(JourneyTask)
        .where(JourneyTask.patient_id == patient_id, JourneyTask.status == "aberta")
        .order_by(JourneyTask.critical.desc(), JourneyTask.due_at)
    ).all()
    out = []
    for t in rows:
        refs = ([t.fhir_ref] if t.fhir_ref else []) + [r for r in (t.source_refs or []) if not r.startswith("Fala/")]
        refs = list(dict.fromkeys(refs)) or [f"Patient/{patient_id}"]
        motivo = [r for r in (t.source_refs or []) if r.startswith("Fala/")]
        origem = (
            (f"consulta #{t.consultation_id}" + (f", {', '.join(motivo)}" if motivo else ""))
            if t.consultation_id
            else "jornada"
        )
        vencida = t.due_at < now
        prazo = t.due_at.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d/%m")
        texto = (
            f"{t.title} — responsável: {PAPEL_LABEL.get(t.role, t.role)}, prazo {prazo}"
            f"{' (vencida)' if vencida else ''} ({origem})"
        )
        publico = (
            ["medico", "enfermagem"]
            if (t.critical or t.role == "gestor")
            else ["medico" if t.role == "medico" else "enfermagem"]
        )
        out.append(
            Fact(
                tipo="tarefa_aberta",
                texto=texto,
                severidade="critico" if t.critical else ("atencao" if vencida else "info"),
                refs=refs,
                publico=publico,
            )
        )
    return out
