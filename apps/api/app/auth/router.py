import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth import tokens
from app.auth.deps import CSRF_COOKIE, SESSION_COOKIE, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.db.models import Organization, User
from app.db.session import get_db
from app.email import send_email
from app.planos import service as planos
from app.prontidao import cookie_seguro

router = APIRouter(prefix="/auth", tags=["auth"])

DEMO_PASSWORD = "housia123"
DEMO_ORG_NAME = "Hospital Demonstração"
DEMO_USERS = [
    ("Dra. Helena Prado", "medico@housia.dev", "medico"),
    ("Enf. Marcos Lima", "enfermeira@housia.dev", "enfermagem"),
    ("Ana Gestora", "gestor@housia.dev", "gestor"),
]
CONSELHOS = ("CRM", "COREN", "CRO", "CRF", "CREFITO", "CRN", "CRP", "CREFONO")
UFS = "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
ORG_TIPOS = ("consultorio", "hospital")
MIN_SENHA = 8


class LoginIn(BaseModel):
    email: str
    password: str


class OrgOut(BaseModel):
    id: int
    nome: str
    tipo: str


class PlanoOut(BaseModel):
    codigo: str
    nome: str
    modulos: list[str]  # módulos hospitalares liberados (passagem_plantao, reconciliacao, alta)
    limite: int | None = None
    usadas: int = 0
    aviso: str | None = None


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    organizacao: OrgOut | None = None
    plano: PlanoOut | None = None
    destino: str = "/leitos"  # para onde o login leva: consultório /consultas, hospital /leitos
    conselho: str | None = None
    conselho_numero: str | None = None
    uf: str | None = None
    conselho_verificado: bool = False
    email_verificado: bool = True


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class RegisterIn(BaseModel):
    nome: str = Field(min_length=3, max_length=120)
    email: str = Field(max_length=200)
    senha: str = Field(max_length=200)
    organizacao_nome: str = Field(min_length=2, max_length=160)
    organizacao_tipo: str = "consultorio"
    conselho: str | None = None
    conselho_numero: str | None = Field(default=None, max_length=20)
    uf: str | None = None


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ForgotIn(BaseModel):
    email: str


class ResetIn(TokenIn):
    senha: str = Field(max_length=200)


class AcceptInviteIn(TokenIn):
    nome: str = Field(min_length=3, max_length=120)
    senha: str = Field(max_length=200)
    conselho: str | None = None
    conselho_numero: str | None = Field(default=None, max_length=20)
    uf: str | None = None


def destino_de(org: Organization | None) -> str:
    return "/consultas" if org is not None and org.tipo == "consultorio" else "/leitos"


def to_user_out(user: User, db: Session | None = None) -> UserOut:
    org = None
    if user.organization_id and db is not None:
        org = db.get(Organization, user.organization_id)
    plano = None
    if org is not None and db is not None:
        u = planos.uso(db, org)  # o contador da sessão é o da organização do usuário
        plano = PlanoOut(
            codigo=u["plano"],
            nome=u["nome"],
            modulos=u["modulos"],
            limite=u["limite"],
            usadas=u["usadas"],
            aviso=u["aviso"],
        )
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        organizacao=OrgOut(id=org.id, nome=org.nome, tipo=org.tipo) if org else None,
        plano=plano,
        destino=destino_de(org),
        conselho=user.conselho,
        conselho_numero=user.conselho_numero,
        uf=user.uf,
        conselho_verificado=user.conselho_verificado,
        email_verificado=user.email_verificado,
    )


def default_org(db: Session) -> Organization:
    org = db.scalar(select(Organization).where(Organization.nome == DEMO_ORG_NAME).order_by(Organization.id))
    if org is None:
        org = Organization(nome=DEMO_ORG_NAME, tipo="hospital")
        db.add(org)
        db.flush()
    return org


def seed_demo_users(db: Session) -> None:
    org = default_org(db)
    for name, email, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(name=name, email=email, role=role, password_hash=hash_password(DEMO_PASSWORD))
            db.add(user)
        user.organization_id = user.organization_id or org.id
        user.email_verificado = True
        if role == "medico" and not user.conselho:  # número fictício, para o selo "verificado" aparecer na demonstração
            user.conselho, user.conselho_numero, user.uf, user.conselho_verificado = "CRM", "123456", "SP", True
    db.commit()


def _validar_senha(senha: str) -> None:
    if len(senha) < MIN_SENHA:
        raise HTTPException(422, f"A senha precisa ter ao menos {MIN_SENHA} caracteres")


def _validar_conselho(conselho: str | None, numero: str | None, uf: str | None, obrigatorio: bool) -> None:
    if not any((conselho, numero, uf)):
        if obrigatorio:
            raise HTTPException(422, "Informe conselho, número e UF")
        return
    if conselho not in CONSELHOS or uf not in UFS or not (numero and numero.isdigit()):
        raise HTTPException(
            422,
            f"Conselho inválido: use um de {', '.join(CONSELHOS)}, número só com dígitos e UF de duas letras",
        )


def _link(path: str, token: str) -> str:
    return f"{get_settings().app_base_url.rstrip('/')}{path}?token={token}"


def _mandar_verificacao(db: Session, user: User) -> None:
    raw = tokens.issue(db, "verify", user_id=user.id)
    send_email(
        user.email,
        "Confirme seu e-mail no HousIA",
        f"Olá, {user.name}.\n\nConfirme seu e-mail para ativar a conta:\n{_link('/verificar', raw)}\n\n"
        "O link vale por 48 horas. Se você não pediu o cadastro, ignore esta mensagem.",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> dict:
    """Autocadastro: cria a organização e o primeiro usuário (consultório: médico; hospital: gestor)."""
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(422, "E-mail inválido")
    if body.organizacao_tipo not in ORG_TIPOS:
        raise HTTPException(422, "Tipo de organização inválido")
    _validar_senha(body.senha)
    consultorio = body.organizacao_tipo == "consultorio"
    _validar_conselho(body.conselho, body.conselho_numero, body.uf, obrigatorio=consultorio)
    nome_org = body.organizacao_nome.strip()
    if nome_org.casefold() == DEMO_ORG_NAME.casefold():  # a organização padrão é a da plataforma (administração)
        raise HTTPException(422, "Nome de organização reservado")
    resposta = {"detail": "Cadastro criado. Enviamos um link de confirmação para o seu e-mail."}
    existente = db.scalar(select(User).where(User.email == email))
    if existente is not None:
        # mesma resposta de um cadastro novo (não revela quem tem conta); o dono do e-mail recebe o aviso
        send_email(
            existente.email,
            "Você já tem uma conta no HousIA",
            f"Olá, {existente.name}.\n\nAlguém tentou criar um cadastro com este e-mail, mas ele já tem conta. "
            'Se foi você, entre normalmente ou use "Esqueci a senha" '
            f"({get_settings().app_base_url.rstrip('/')}/esqueci). "
            "Se não foi, ignore esta mensagem.",
        )
        return resposta
    org = Organization(nome=nome_org, tipo=body.organizacao_tipo)
    db.add(org)
    db.flush()
    user = User(
        name=body.nome.strip(),
        email=email,
        password_hash=hash_password(body.senha),
        role="medico" if consultorio else "gestor",
        organization_id=org.id,
        conselho=body.conselho,
        conselho_numero=body.conselho_numero,
        uf=body.uf,
        email_verificado=False,
    )
    db.add(user)
    db.commit()
    log_action(db, user, "auth.register", "user", user.id, {"organizacao": org.id, "tipo": org.tipo})
    _mandar_verificacao(db, user)
    return resposta


@router.post("/verify")
def verify(body: TokenIn, db: Session = Depends(get_db)) -> dict:
    tok = tokens.consume(db, "verify", body.token)
    user = db.get(User, tok.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Link inválido, já usado ou expirado")
    user.email_verificado = True
    db.commit()
    log_action(db, user, "auth.verify", "user", user.id)
    return {"detail": "E-mail confirmado. Você já pode entrar."}


@router.post("/forgot")
def forgot(body: ForgotIn, db: Session = Depends(get_db)) -> dict:
    """Resposta idêntica exista ou não a conta (não revela quem tem cadastro)."""
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is not None:
        tokens.burn_open(db, "reset", user.id)  # só o link mais recente vale
        raw = tokens.issue(db, "reset", user_id=user.id)
        send_email(
            user.email,
            "Redefinição de senha do HousIA",
            f"Olá, {user.name}.\n\nPara criar uma nova senha acesse:\n{_link('/redefinir', raw)}\n\n"
            "O link vale por 1 hora e só pode ser usado uma vez. Se não foi você, ignore esta mensagem.",
        )
    return {"detail": "Se o e-mail estiver cadastrado, enviamos o link para redefinir a senha."}


@router.post("/reset")
def reset(body: ResetIn, db: Session = Depends(get_db)) -> dict:
    _validar_senha(body.senha)
    tok = tokens.consume(db, "reset", body.token)
    user = db.get(User, tok.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Link inválido, já usado ou expirado")
    user.password_hash = hash_password(body.senha)
    tokens.burn_open(db, "reset", user.id)  # os demais links de redefinição pendentes morrem junto
    user.email_verificado = True  # quem recebeu o link no e-mail provou que é dono dele
    db.commit()
    log_action(db, user, "auth.reset", "user", user.id)
    return {"detail": "Senha redefinida. Entre com a nova senha."}


@router.get("/invites/{token}")
def invite_info(token: str, db: Session = Depends(get_db)) -> dict:
    """Dados do convite para a tela de aceite (não queima o token)."""
    tok = tokens.peek(db, "invite", token)
    org = db.get(Organization, tok.organization_id)
    return {"email": tok.email, "papel": tok.role, "organizacao": org.nome if org else ""}


@router.post("/invites/accept", status_code=status.HTTP_201_CREATED)
def accept_invite(body: AcceptInviteIn, db: Session = Depends(get_db)) -> dict:
    """Aceita o convite: cria o usuário com o papel escolhido por quem convidou, já com o e-mail confirmado."""
    _validar_senha(body.senha)
    _validar_conselho(body.conselho, body.conselho_numero, body.uf, obrigatorio=False)
    tok = tokens.consume(db, "invite", body.token)
    if db.scalar(select(User.id).where(User.email == tok.email)) is not None:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Já existe uma conta com este e-mail")
    user = User(
        name=body.nome.strip(),
        email=tok.email,
        password_hash=hash_password(body.senha),
        role=tok.role,
        organization_id=tok.organization_id,
        conselho=body.conselho,
        conselho_numero=body.conselho_numero,
        uf=body.uf,
        email_verificado=True,
    )
    db.add(user)
    db.commit()
    log_action(db, user, "auth.convite_aceito", "user", user.id, {"papel": user.role})
    return {"detail": "Conta criada. Você já pode entrar.", "user_id": user.id}


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou senha inválidos")
    if not user.email_verificado:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Confirme seu e-mail pelo link que enviamos antes de entrar")
    log_action(db, user, "auth.login", "user", user.id)
    token = create_access_token(user.id, user.role)
    set_session_cookies(response, token)
    return LoginOut(access_token=token, user=to_user_out(user, db))


def set_session_cookies(response: Response, token: str) -> None:
    """Sessão em cookie httpOnly SameSite=strict (o JS não lê o token) + token CSRF legível (double submit)."""
    max_age = get_settings().jwt_expires_minutes * 60
    secure = cookie_seguro()
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="strict", secure=secure, path="/"
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


@router.get("/me", response_model=UserOut)
def me(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    # sessão por cookie sem o token CSRF (cookie apagado): reemite, senão toda escrita daria 403 até um novo login
    if request.cookies.get(SESSION_COOKIE) and not request.cookies.get(CSRF_COOKIE):
        response.set_cookie(
            CSRF_COOKIE,
            secrets.token_urlsafe(32),
            max_age=get_settings().jwt_expires_minutes * 60,
            httponly=False,
            samesite="strict",
            secure=cookie_seguro(),
            path="/",
        )
    return to_user_out(user, db)
