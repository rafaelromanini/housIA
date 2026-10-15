from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.db.models import AIFeedback, User
from app.db.session import get_db

router = APIRouter(tags=["feedback da IA"])

Kind = Literal["handoff", "handoff_check", "medrec", "discharge", "discharge_docs"]


class FeedbackIn(BaseModel):
    kind: Kind
    entity_id: str | None = None
    patient_id: str | None = None
    useful: bool
    comment: str | None = Field(None, max_length=1000)


@router.post("/feedback")
def post_feedback(body: FeedbackIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Registra se a saída da IA foi útil (métrica de qualidade do piloto)."""
    if not body.useful and not (body.comment or "").strip():
        raise HTTPException(422, "Conte rapidamente o que não ajudou (ex.: faltou informação, item errado).")
    fb = AIFeedback(
        user_id=user.id,
        patient_id=body.patient_id,
        kind=body.kind,
        entity_id=body.entity_id,
        useful=body.useful,
        comment=(body.comment or "").strip() or None,
    )
    db.add(fb)
    db.flush()
    log_action(
        db,
        user,
        "ia.feedback",
        body.kind,
        body.entity_id,
        {
            "patient_id": body.patient_id,
            "util": body.useful,
            "comentario": fb.comment,
            "feedback_id": fb.id,
        },
    )
    return {"id": fb.id, "kind": fb.kind, "useful": fb.useful}
