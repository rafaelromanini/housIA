from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.db.models import Alert

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
                log_action(db, None, "alerta.reaberto", "alert", alert.id, {"severidade": severity}, commit=False)
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
