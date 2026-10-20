"""Isolamento por organização (#33).

A dependência de autenticação grava a organização do usuário em `db.info["org_id"]`. A partir daí, toda consulta
ORM às tabelas com dono (mistura `Tenant`) recebe `WHERE organization_id = :org`, e todo registro novo sai com a
organização preenchida. Sessões sem organização (subida da API, expurgo, rekey, testes de serviço) não filtram.
Uma consulta de outra organização não existe para o usuário: o endpoint responde 404, nunca 403.
"""

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.db.models import Tenant

ORG_KEY = "org_id"


def set_org(db: Session, org_id: int | None) -> None:
    if org_id is None:
        db.info.pop(ORG_KEY, None)
    else:
        db.info[ORG_KEY] = org_id


def current_org(db: Session) -> int | None:
    return db.info.get(ORG_KEY)


@event.listens_for(Session, "do_orm_execute")
def _filter_by_org(state) -> None:
    org_id = state.session.info.get(ORG_KEY)
    if org_id is None or not state.is_select:
        return
    state.statement = state.statement.options(
        with_loader_criteria(Tenant, lambda cls: cls.organization_id == org_id, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _stamp_org(session: Session, _ctx, _instances) -> None:
    org_id = session.info.get(ORG_KEY)
    if org_id is None:
        return
    for obj in (*session.new, *session.dirty):
        if not isinstance(obj, Tenant):
            continue
        if obj.organization_id is None:
            obj.organization_id = org_id
        elif obj.organization_id != org_id:  # nenhum caminho legítimo grava ou move registro para outra organização
            raise PermissionError("organization_id de outra organização")
