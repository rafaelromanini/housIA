import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import require_roles
from app.compliance import service
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/compliance", tags=["conformidade"])


@router.get("/dossie")
def dossie(
    formato: str = Query("json", pattern="^(json|md)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("gestor")),
) -> Response:
    """RoPA, RIPD resumido e matriz CFM/LGPD, com versão, data e as partes calculadas do sistema. Só o gestor."""
    d = service.montar(db, user)
    log_action(
        db,
        user,
        "compliance.dossie_gerado",
        "compliance",
        d["versao"],
        {"formato": formato, "cadeia_integra": d["calculado"]["cadeia_audit_log"]["integra"]},
    )
    headers = {"Cache-Control": "no-store"}
    if formato == "md":
        nome = f"dossie-conformidade-{d['versao']}.md"
        return Response(
            service.markdown(d).encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers=headers | {"Content-Disposition": f'attachment; filename="{nome}"'},
        )
    return Response(json.dumps(d, ensure_ascii=False).encode("utf-8"), media_type="application/json", headers=headers)
