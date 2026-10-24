"""Tarefas da consulta (#47): grava as propostas de `tarefas.py` como JourneyTask + FHIR Task, sem duplicar."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.consult import tarefas
from app.consult.schemas import Insights, Segment
from app.db.models import Consultation, JourneyTask, User
from app.fhir_client.client import FhirSource
from app.journey import reference
from app.journey.service import task_resource

logger = logging.getLogger(__name__)
MANUAL = "consulta_manual"


def _focus(c: Consultation) -> str:
    return f"Encounter/{c.encounter_id}" if c.encounter_id else f"Patient/{c.patient_id}"


def _reason(c: Consultation, refs: list[str]) -> str:
    falas = ", ".join(r for r in refs if r.startswith("Fala/"))
    return f"Consulta #{c.id}" + (f", {falas}" if falas else "")


def _persist(
    db: Session, fhir: FhirSource, user: User, c: Consultation, p: tarefas.Proposta, manual: bool = False
) -> JourneyTask:
    now = clock.now()
    focus = _focus(c)
    refs = list(dict.fromkeys([*p.refs, focus]))
    task = JourneyTask(
        patient_id=c.patient_id,
        rule=MANUAL if manual else p.regra,
        title=p.titulo[:300],
        detail=p.detalhe,
        role=p.papel,
        due_at=now + timedelta(hours=p.horas),
        status="aberta",
        source_refs=refs,
        created_by=user.id,
        created_at=now,
        consultation_id=c.id,
        critical=p.critica,
        chave=p.chave,
    )
    db.add(task)
    db.flush()
    try:
        created = fhir.create(task_resource(task, refs, user.name, focus=focus, reason=_reason(c, refs)))
        task.fhir_ref = f"Task/{created['id']}"
    except Exception:  # noqa: BLE001 - o banco é a fonte da verdade; sem o Task a tarefa segue valendo
        logger.warning("Task FHIR da tarefa %s não pôde ser criada.", task.id, exc_info=True)
    log_action(
        db, user, "jornada.tarefa_criada", "journey_task", task.id,
        {"patient_id": c.patient_id, "regra": task.rule, "papel": task.role, "prazo": task.due_at.isoformat(),
         "fhir": task.fhir_ref, "consulta": c.id, "critica": task.critical,
         "origem": "profissional" if manual else "regra", "falante_auto": bool(p.extra.get("falante_auto"))},
        commit=False,
    )  # fmt: skip
    return task


def sincronizar(
    db: Session, fhir: FhirSource, user: User, c: Consultation, segments: list[Segment], ins: Insights
) -> dict:
    """Cria as tarefas que a consulta gerou. Sandbox e cirurgia não geram; a mesma chave não vira duas tarefas, nem
    quando a consulta é finalizada de novo ou quando o protocolo já tem tarefa aberta de uma consulta anterior."""
    if c.sandbox or c.kind == "cirurgia" or c.id is None:
        return {"criadas": 0, "criticas": 0}
    props = tarefas.extrair(segments, ins, c.patient_id)
    mine = {t.chave for t in db.scalars(select(JourneyTask).where(JourneyTask.consultation_id == c.id))}
    open_protocol = {
        t.chave
        for t in db.scalars(
            select(JourneyTask).where(
                JourneyTask.patient_id == c.patient_id,
                JourneyTask.status == "aberta",
                JourneyTask.rule == "consulta_protocolo",
            )
        )
    }
    created: list[JourneyTask] = []
    for p in props:
        if p.chave in mine or (p.regra == "consulta_protocolo" and p.chave in open_protocol):
            continue
        created.append(_persist(db, fhir, user, c, p))
    db.flush()
    auto = sum(1 for p in props if p.extra.get("falante_auto") and any(t.chave == p.chave for t in created))
    return {"criadas": len(created), "criticas": sum(t.critical for t in created), "falante_automatico": auto}


def adicionar(
    db: Session,
    fhir: FhirSource,
    user: User,
    c: Consultation,
    titulo: str,
    papel: str,
    horas: int,
    critica: bool,
    detalhe: str = "",
) -> JourneyTask:
    """Pendência escrita pelo profissional na revisão (origem 'profissional', sem fala): vira tarefa como as demais."""
    if c.sandbox:
        raise HTTPException(409, "Consulta de sandbox não gera tarefas.")
    if c.status == "em_andamento":
        raise HTTPException(409, "Finalize a consulta antes de registrar tarefas.")
    if papel not in reference.PAPEIS:
        raise HTTPException(422, "Papel desconhecido.")
    seq = len(list(db.scalars(select(JourneyTask.id).where(JourneyTask.consultation_id == c.id)))) + 1
    prop = tarefas.Proposta(
        chave=f"manual:{c.id}:{seq}",
        regra=MANUAL,
        titulo=titulo.strip(),
        detalhe=detalhe.strip() or "Pendência registrada pelo profissional na revisão da consulta.",
        papel=papel,
        horas=horas,
        critica=critica,
        refs=[],
    )
    task = _persist(db, fhir, user, c, prop, manual=True)
    db.commit()
    return task


def listar(db: Session, fhir: FhirSource, cid: int) -> dict:
    from app.consult import service  # import tardio: service importa este módulo
    from app.journey.service import list_tasks

    c = service._get(db, cid)
    rows = list_tasks(db, fhir, None, None, consultation_id=cid)
    return {
        "consulta_id": c.id,
        "tarefas": rows,
        "abertas": sum(r["status"] == "aberta" for r in rows),
        "criticas_abertas": sum(r["status"] == "aberta" and r["critica"] for r in rows),
        "aviso": "Pendências que a conversa gerou. A tarefa crítica não pode ser concluída sem justificativa e aparece "
        "na passagem de plantão e na alta.",
    }
