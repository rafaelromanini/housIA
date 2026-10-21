"""Página pública: formulário "Piloto para hospital" (sem login). Cai na auditoria do gestor da plataforma."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.router import default_org
from app.db.session import get_db
from app.db.tenancy import set_org

router = APIRouter(prefix="/public", tags=["publico"])


class PilotoIn(BaseModel):
    hospital: str = Field(min_length=2, max_length=160)
    contato: str = Field(min_length=3, max_length=120)
    email: str = Field(max_length=200)
    leitos: int | None = Field(default=None, ge=1, le=5000)
    mensagem: str = Field(default="", max_length=1000)


@router.post("/piloto", status_code=status.HTTP_201_CREATED)
def pedir_piloto(body: PilotoIn, db: Session = Depends(get_db)) -> dict:
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(422, "E-mail inválido")
    org = default_org(db)
    set_org(db, org.id)  # o registro cai na organização da plataforma, onde o gestor lê a auditoria
    entry = log_action(
        db,
        None,
        "lead.piloto",
        "lead",
        None,
        {
            "hospital": body.hospital,
            "contato": body.contato,
            "email": email,
            "leitos": body.leitos,
            "mensagem": body.mensagem,
        },
        commit=False,
    )
    entry.organization_id = org.id
    db.commit()
    return {"detail": "Recebemos o pedido. Entraremos em contato por e-mail."}
