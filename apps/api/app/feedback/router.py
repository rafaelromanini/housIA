from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.config import get_settings
from app.db.models import AIFeedback, User
from app.db.session import get_db

router = APIRouter(tags=["feedback da IA"])

Kind = Literal["handoff", "handoff_check", "medrec", "discharge", "discharge_docs", "consulta", "consulta_nota", "nps"]


class FeedbackIn(BaseModel):
    kind: Kind
    entity_id: str | None = None
    patient_id: str | None = None
    useful: bool | None = None  # kind "nps" usa a nota
    comment: str | None = Field(None, max_length=1000)
    score: int | None = Field(None, ge=0, le=10, description='Só para kind "nps": de 0 a 10')


@router.post("/feedback")
def post_feedback(body: FeedbackIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Registra se a saída da IA foi útil (métrica de qualidade do piloto) ou, em kind "nps", a nota de 0 a 10."""
    if body.kind == "nps":
        return _post_nps(body, db, user)
    if body.useful is None:
        raise HTTPException(422, "Informe se a saída foi útil.")
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


def _month(moment) -> str:
    return moment.astimezone(ZoneInfo(get_settings().timezone)).strftime("%Y-%m")


def current_nps(db: Session, user: User) -> AIFeedback | None:
    """A resposta do profissional neste mês (fuso do hospital): uma pergunta por mês."""
    month = _month(clock.now())
    rows = db.scalars(
        select(AIFeedback).where(AIFeedback.user_id == user.id, AIFeedback.kind == "nps").order_by(AIFeedback.id.desc())
    ).all()
    return next((r for r in rows if _month(r.created_at) == month), None)


def _post_nps(body: FeedbackIn, db: Session, user: User) -> dict:
    """NPS de uma pergunta ("de 0 a 10, o quanto recomendaria?"): uma nota por profissional por mês; responder de novo
    no mês troca a nota (a anterior continua na auditoria)."""
    if body.score is None:
        raise HTTPException(422, "Informe a nota de 0 a 10.")
    fb = current_nps(db, user)
    if fb is None:
        fb = AIFeedback(user_id=user.id, kind="nps", created_at=clock.now())
        db.add(fb)
    fb.score, fb.useful = body.score, body.score >= 9
    fb.comment = (body.comment or "").strip() or None
    fb.created_at = clock.now()
    db.flush()
    log_action(
        db,
        user,
        "ia.feedback",
        "nps",
        fb.id,
        {"nota": body.score, "comentario": fb.comment, "feedback_id": fb.id, "mes": _month(fb.created_at)},
    )
    return {"id": fb.id, "kind": "nps", "score": fb.score}


@router.get("/feedback/nps")
def get_nps(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """O profissional já respondeu o NPS deste mês? (a tela só pergunta uma vez)"""
    fb = current_nps(db, user)
    return {"mes": _month(clock.now()), "respondido": fb is not None, "nota": fb.score if fb else None}
