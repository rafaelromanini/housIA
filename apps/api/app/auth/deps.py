import hmac
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.security import decode_access_token
from app.db.models import User
from app.db.session import get_db

ROLES = ("medico", "enfermagem", "gestor")

_bearer = HTTPBearer(auto_error=False)

SESSION_COOKIE = "housia_session"
CSRF_COOKIE = "housia_csrf"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is not None:  # clientes de API: Authorization não carrega credencial ambiente, sem CSRF
        token = creds.credentials
    else:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Não autenticado")
        if request.method not in _SAFE_METHODS:  # cookie é credencial ambiente: exige o token CSRF (double submit)
            sent = request.headers.get(CSRF_HEADER, "")
            if not sent or not hmac.compare_digest(sent, request.cookies.get(CSRF_COOKIE, "")):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Token CSRF ausente ou inválido")
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido ou expirado") from exc
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuário não encontrado")
    return user


def require_roles(*roles: str) -> Callable[[User], User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Ação restrita ao(s) perfil(is): {', '.join(roles)}",
            )
        return user

    return checker
