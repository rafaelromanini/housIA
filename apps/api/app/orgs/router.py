"""Organização: convites de equipe, membros e conferência manual do conselho profissional (#33)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth import tokens
from app.auth.deps import ROLES, get_current_user, get_org, require_roles
from app.auth.router import UserOut, default_org, to_user_out
from app.br import identificadores as br_ids
from app.config import get_settings
from app.db.models import AuthToken, Consultation, Organization, User
from app.db.session import get_db
from app.email import send_email
from app.planos import service as planos

router = APIRouter(tags=["organizacoes"])
gestor_only = require_roles("gestor")


class InviteIn(BaseModel):
    email: str = Field(max_length=200)
    papel: str


class ConselhoIn(BaseModel):
    verificado: bool = True


class CadastroOrgIn(BaseModel):
    cnes: str | None = Field(default=None, max_length=20)  # None ou vazio apaga


class CadastroProfIn(BaseModel):
    cns: str | None = Field(default=None, max_length=30)
    cbo: str | None = Field(default=None, max_length=20)


class PlanoIn(BaseModel):
    plano: str
    vigencia_ate: datetime | None = None
    limite_consultas: StrictInt | None = Field(default=None, ge=1)
    modulos: list[str] | None = None


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


def _validar(fn, valor):
    """Vazio apaga o campo; o resto passa pelo validador brasileiro (erro vira 422 em português)."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    try:
        return fn(valor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/orgs/{org_id}/cadastro")
def cadastro_org(
    org_id: int,
    body: CadastroOrgIn,
    user: User = Depends(gestor_only),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> dict:
    """CNES do estabelecimento (#52): vai para o Bundle (perfil BR Core) e para a exportação LEDI."""
    org = _alvo(db, org_id, user, mine)
    org.cnes = _validar(br_ids.validar_cnes, body.cnes)
    db.commit()
    log_action(db, user, "org.cadastro_br", "organization", org.id, {"campo": "cnes", "preenchido": bool(org.cnes)})
    return {"id": org.id, "cnes": org.cnes}


@router.put("/orgs/{org_id}/membros/{user_id}/cadastro")
def cadastro_profissional(
    org_id: int,
    user_id: int,
    body: CadastroProfIn,
    user: User = Depends(gestor_only),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> dict:
    """CNS e CBO do profissional (#52). Validados (dígito verificador do CNS); vazio apaga."""
    org = _alvo(db, org_id, user, mine)
    membro = db.get(User, user_id)
    if membro is None or membro.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profissional não encontrado")
    cns, cbo = _validar(br_ids.validar_cns, body.cns), _validar(br_ids.validar_cbo, body.cbo)
    membro.cns, membro.cbo = cns, cbo
    db.commit()
    log_action(db, user, "org.cadastro_br", "user", membro.id, {"campo": "cns_cbo", "cns": bool(cns), "cbo": bool(cbo)})
    return {"id": membro.id, "cns": membro.cns, "cbo": membro.cbo}


@router.get("/orgs/{org_id}/plano")
def ver_plano(
    org_id: int, user: User = Depends(gestor_only), mine: Organization = Depends(get_org), db: Session = Depends(get_db)
) -> dict:
    org = _alvo(db, org_id, user, mine)
    resp = planos.uso(db, org)
    db.commit()  # a leitura cria o direito de uso inicial quando a organização ainda não tem
    return resp


@router.put("/orgs/{org_id}/plano")
def trocar_plano(
    org_id: int,
    body: PlanoIn,
    user: User = Depends(gestor_only),
    mine: Organization = Depends(get_org),
    db: Session = Depends(get_db),
) -> dict:
    """Ativação manual do plano: só o administrador da plataforma (gestor da organização padrão) troca."""
    if mine.id != default_org(db).id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Só o administrador da plataforma troca o plano")
    org = _alvo(db, org_id, user, mine)
    antes = planos.direito(db, org).plano
    planos.trocar(
        db, org, user, body.plano, vigencia_ate=body.vigencia_ate, limite=body.limite_consultas, modulos=body.modulos
    )
    db.commit()
    log_action(db, user, "org.plano", "organization", org.id, {"de": antes, "para": body.plano})
    return planos.uso(db, org)
