"""Organização: convites de equipe, membros e conferência manual do conselho profissional (#33)."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth import tokens
from app.auth.deps import ROLES, get_current_user, get_org, require_roles
from app.auth.router import UserOut, default_org, to_user_out
from app.config import get_settings
from app.db.models import AuthToken, Consultation, Organization, User
from app.db.session import get_db
from app.email import send_email

router = APIRouter(tags=["organizacoes"])
gestor_only = require_roles("gestor")


class InviteIn(BaseModel):
    email: str = Field(max_length=200)
    papel: str


class ConselhoIn(BaseModel):
    verificado: bool = True


def _alvo(db: Session, org_id: int, user: User, mine: Organization) -> Organization:
    """Organização que o gestor pode administrar: a sua; o administrador da plataforma (gestor da organização
    padrão) confere conselhos de qualquer uma. Qualquer outra responde 404, como se não existisse."""
    if org_id == mine.id:
        return mine
    if mine.id == default_org(db).id:
        org = db.get(Organization, org_id)
        if org is not None:
            return org
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Organização não encontrada")


@router.get("/orgs/{org_id}/onboarding")
def onboarding(
    org_id: int,
    user: User = Depends(get_current_user),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> dict:
    """Passos do onboarding, que termina na primeira nota aprovada (revisada e atestada) da organização."""
    if org_id != mine.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organização não encontrada")
    criada = aprovada = False
    for c in db.scalars(select(Consultation).order_by(Consultation.id)):  # já filtrado pela organização
        criada = True
        if (c.note_meta_json or {}).get("atestacao"):
            aprovada = True
            break
    passos = [
        {"chave": "conta", "titulo": "Criar a conta", "feito": True},
        {"chave": "email", "titulo": "Confirmar o e-mail", "feito": user.email_verificado},
        {
            "chave": "conselho",
            "titulo": "Informar o registro no conselho (CRM, COREN...)",
            "feito": bool(user.conselho and user.conselho_numero and user.uf),
        },
        {"chave": "consulta", "titulo": "Iniciar a primeira consulta", "feito": criada},
        {"chave": "nota", "titulo": "Aprovar a primeira nota", "feito": aprovada},
    ]
    return {
        "passos": passos,
        "concluido": all(p["feito"] for p in passos),
        "conselho_verificado": user.conselho_verificado,
    }


@router.get("/orgs/{org_id}/membros", response_model=list[UserOut])
def membros(
    org_id: int, user: User = Depends(gestor_only), mine: Organization = Depends(get_org), db: Session = Depends(get_db)
) -> list[UserOut]:
    org = _alvo(db, org_id, user, mine)
    rows = db.scalars(select(User).where(User.organization_id == org.id).order_by(User.id))
    return [to_user_out(u, db) for u in rows]


@router.post("/orgs/{org_id}/invites", status_code=status.HTTP_201_CREATED)
def convidar(
    org_id: int,
    body: InviteIn,
    user: User = Depends(gestor_only),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> dict:
    if org_id != mine.id:  # só convida para a própria organização
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organização não encontrada")
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(422, "E-mail inválido")
    if body.papel not in ROLES:
        raise HTTPException(422, f"Papel inválido: use {', '.join(ROLES)}")
    resposta = {"detail": "Convite enviado", "email": email, "papel": body.papel}
    existente = db.scalar(select(User).where(User.email == email))
    if existente is not None:
        if existente.organization_id == mine.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Esta pessoa já faz parte da equipe")
        return resposta  # conta de outra organização: não revelamos que existe nem enviamos convite
    raw = tokens.issue(db, "invite", organization_id=mine.id, email=email, role=body.papel)
    base = get_settings().app_base_url.rstrip("/")
    send_email(
        email,
        f"Convite para a equipe {mine.nome} no HousIA",
        f"{user.name} convidou você para a organização {mine.nome} como {body.papel}.\n\n"
        f"Crie sua conta pelo link (vale por 7 dias):\n{base}/convite?token={raw}\n",
    )
    log_action(db, user, "org.convite", "organization", mine.id, {"papel": body.papel})
    return resposta


@router.get("/orgs/{org_id}/invites")
def convites_pendentes(
    org_id: int, user: User = Depends(gestor_only), mine: Organization = Depends(get_org), db: Session = Depends(get_db)
) -> list[dict]:
    if org_id != mine.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organização não encontrada")
    rows = db.scalars(
        select(AuthToken).where(AuthToken.kind == "invite", AuthToken.organization_id == mine.id).order_by(AuthToken.id)
    )
    return [{"email": t.email, "papel": t.role, "aceito": t.used_at is not None} for t in rows]


@router.post("/orgs/{org_id}/membros/{user_id}/conselho", response_model=UserOut)
def conferir_conselho(
    org_id: int,
    user_id: int,
    body: ConselhoIn,
    user: User = Depends(gestor_only),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> UserOut:
    """Conferência manual do registro no conselho (consulta ao portal do CRM/COREN): liga o selo "verificado"."""
    org = _alvo(db, org_id, user, mine)
    membro = db.get(User, user_id)
    if membro is None or membro.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profissional não encontrado")
    if body.verificado and not (membro.conselho and membro.conselho_numero and membro.uf):
        raise HTTPException(422, "O profissional não informou conselho, número e UF")
    membro.conselho_verificado = body.verificado
    db.commit()
    log_action(db, user, "org.conselho", "user", membro.id, {"verificado": body.verificado, "organizacao": org.id})
    return to_user_out(membro, db)
