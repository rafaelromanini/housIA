"""Modo consultório (#34): plano de teste grátis, sandbox, busca global de consultas e fila de notas para revisar.

O sandbox usa os cenários sintéticos do HousIA numa consulta marcada (`Consultation.sandbox`): ela não entra no
contador do plano, não gera acompanhamento na jornada e nunca é gravada no prontuário. A busca roda sobre as
consultas da organização (o filtro por organização da sessão já vale) e lê o CID da nota, que fica cifrada.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.consult import knowledge
from app.db.models import Consultation, User
from app.db.tenancy import current_org
from app.patients import avulso
from app.planos import service as planos
from app.timeline.record import PatientRecord

AVISO_SANDBOX = "SANDBOX — cenário sintético, sem validade clínica"
STATUS = ("em_andamento", "finalizada", "enviada")
MAX_PAGE = 200


def selo_sandbox(selo: str | None) -> str:
    return AVISO_SANDBOX + (f" · {selo}" if selo else "")


def is_reviewed(c: Consultation) -> bool:
    """Revisada = o profissional atestou a nota (PUT /note com atestação)."""
    return bool((c.note_meta_json or {}).get("atestacao"))


def plano(db: Session) -> dict:
    """Contador do plano da organização (#35): só consulta real conta; sandbox fica de fora."""
    return planos.uso(db)


def check_start(db: Session, record: PatientRecord, sandbox: bool) -> None:
    """Antes de abrir a consulta: sandbox só em cenário sintético; consulta real respeita o plano."""
    if sandbox:
        if avulso.is_avulso(record.id) or not (record.scenario_tag or "").startswith("cenario"):
            raise HTTPException(422, "O sandbox usa só os cenários sintéticos; paciente real não entra nele.")
        return
    planos.confere_inicio(db)


def scenarios(fhir) -> list[dict]:
    """Cenários sintéticos do sandbox: um roteiro de demonstração por paciente sintético marcado."""
    out = []
    for s in knowledge.demo_scripts().values():
        patient = fhir.get("Patient", s.get("patient_id") or "")
        if patient is None:
            continue
        out.append(
            {
                "id": s["id"],
                "patient_id": patient["id"],
                "paciente": avulso.name_of(patient),
                "titulo": s.get("titulo"),
                "tipo": s.get("tipo"),
                "especialidade": s.get("especialidade"),
                "especialidade_rotulo": knowledge.SPECIALTIES.get(s.get("especialidade") or "", s.get("especialidade")),
                "sintetico": True,
            }
        )
    return out


# ---------- busca global ----------
def _norm_cid(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def cids_of(c: Consultation) -> list[dict]:
    """CID-10 da consulta: os da nota (revisados) ou, enquanto não há nota, as sugestões da análise."""
    note = c.note_json or {}
    report = note.get("relatorio_cirurgico", note) if c.kind == "cirurgia" else note
    items = report.get("cids") or (c.insights_json or {}).get("cids") or []
    return [{"codigo": i.get("codigo", ""), "descricao": i.get("descricao", "")} for i in items if i.get("codigo")]


def _haystack(c: Consultation, nome: str, norm) -> str:
    cids = " ".join(f"{i['codigo']} {i['descricao']}" for i in cids_of(c))
    return norm(f"{nome} {c.patient_id} {knowledge.SPECIALTIES.get(c.specialty, c.specialty)} {cids}")


def _day(dt: datetime) -> date:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).date()


def _row(c: Consultation, nome: str, autores: dict[int, str]) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "paciente": nome,
        "tipo": c.kind,
        "especialidade": c.specialty,
        "especialidade_rotulo": knowledge.SPECIALTIES.get(c.specialty, c.specialty),
        "status": c.status,
        "revisada": is_reviewed(c),
        "aguardando_revisao": c.status == "finalizada" and not is_reviewed(c),
        "sandbox": bool(c.sandbox),
        "cids": cids_of(c),
        "falas": len(c.segments_json or []),
        "criado_por": autores.get(c.created_by or 0),
        "created_at": c.created_at.isoformat(),
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
    }


def search(
    db: Session,
    fhir,
    *,
    status: str | None = None,
    q: str = "",
    cid: str = "",
    de: date | None = None,
    ate: date | None = None,
    revisada: bool | None = None,
    sandbox: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Busca global da organização. `status` aceita também `aguardando_revisao` (finalizada e não atestada)."""
    from app.consult.text import norm

    fila = status == "aguardando_revisao"
    if status and not fila and status not in STATUS:
        raise HTTPException(422, f"Status inválido: use {', '.join((*STATUS, 'aguardando_revisao'))}")
    if de and ate and de > ate:
        raise HTTPException(422, "A data inicial é depois da final")
    stmt = select(Consultation).order_by(Consultation.id.desc())
    if status and not fila:
        stmt = stmt.where(Consultation.status == status)
    if fila:
        stmt = stmt.where(Consultation.status == "finalizada")
    if sandbox is not None:
        stmt = stmt.where(Consultation.sandbox.is_(sandbox))
    rows = list(db.scalars(stmt))
    rows = [c for c in rows if (de is None or _day(c.created_at) >= de) and (ate is None or _day(c.created_at) <= ate)]
    if fila:
        rows = [c for c in rows if not is_reviewed(c)]
    if revisada is not None:
        rows = [c for c in rows if is_reviewed(c) == revisada]
    wanted = _norm_cid(cid)
    if wanted:
        rows = [c for c in rows if any(_norm_cid(i["codigo"]).startswith(wanted) for i in cids_of(c))]
    names: dict[str, str] = {}
    for pid in {c.patient_id for c in rows}:
        patient = fhir.get("Patient", pid)
        names[pid] = avulso.name_of(patient) if patient else pid
    tokens = norm(q).split()
    if tokens:  # cada palavra da busca precisa aparecer em algum lugar da consulta (nome, id, especialidade, CID)
        hay = {c.id: _haystack(c, names[c.patient_id], norm) for c in rows}
        rows = [c for c in rows if all(t in hay[c.id] for t in tokens)]
    autores = {u.id: u.name for u in db.scalars(select(User).where(User.organization_id == current_org(db)))}
    limit = max(1, min(limit, MAX_PAGE))
    page = rows[offset : offset + limit]
    return {
        "total": len(rows),
        "limite": limit,
        "deslocamento": offset,
        "itens": [_row(c, names[c.patient_id], autores) for c in page],
    }


# ---------- onboarding de 3 passos ----------
def onboarding(db: Session) -> dict:
    """Três passos que terminam na primeira nota aprovada no sandbox (sem gastar o teste grátis)."""
    iniciada = finalizada = aprovada = False
    for c in db.scalars(select(Consultation).where(Consultation.sandbox.is_(True))):
        iniciada = True
        finalizada = finalizada or c.status in ("finalizada", "enviada")
        aprovada = aprovada or is_reviewed(c)
    passos = [
        {"chave": "cenario", "titulo": "Escolher um cenário do sandbox e iniciar a consulta", "feito": iniciada},
        {"chave": "nota", "titulo": "Encerrar a consulta e gerar a nota", "feito": finalizada},
        {"chave": "aprovar", "titulo": "Revisar e aprovar a primeira nota", "feito": aprovada},
    ]
    return {"passos": passos, "concluido": all(p["feito"] for p in passos)}
