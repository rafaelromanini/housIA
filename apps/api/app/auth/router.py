import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import CSRF_COOKIE, SESSION_COOKIE, get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.prontidao import cookie_seguro

router = APIRouter(prefix="/auth", tags=["auth"])

DEMO_PASSWORD = "housia123"
DEMO_USERS = [
    ("Dra. Helena Prado", "medico@housia.dev", "medico"),
    ("Enf. Marcos Lima", "enfermeira@housia.dev", "enfermagem"),
    ("Ana Gestora", "gestor@housia.dev", "gestor"),
]


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def to_user_out(user: User) -> UserOut:
    return UserOut(id=user.id, name=user.name, email=user.email, role=user.role)


def seed_demo_users(db: Session) -> None:
    for name, email, role in DEMO_USERS:
        if db.scalar(select(User).where(User.email == email)) is None:
            db.add(User(name=name, email=email, role=role, password_hash=hash_password(DEMO_PASSWORD)))
    db.commit()


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou senha inválidos")
    log_action(db, user, "auth.login", "user", user.id)
    token = create_access_token(user.id, user.role)
    set_session_cookies(response, token)
    return LoginOut(access_token=token, user=to_user_out(user))


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
def me(request: Request, response: Response, user: User = Depends(get_current_user)) -> UserOut:
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
    return to_user_out(user)
