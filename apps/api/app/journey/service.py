"""Waze da jornada: lista de pacientes em risco de abandono, tarefas, contato pós-alta e texto pronto."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.alerts.service import upsert_alert
from app.audit.service import log_action
from app.db.models import Consultation, DischargeAssessment, JourneyContact, JourneyTask, MedrecRun, User
from app.db.tenancy import current_org
from app.discharge.documents import HOME_ROUTES
from app.fhir_client import codes
from app.fhir_client.client import FhirSource
from app.journey import reference
from app.journey.rules import SEVERITY_RANK, Facts, Item, discharge_of, evaluate, local_day
from app.patients import avulso
from app.patients.service import load_record
from app.timeline.record import PatientRecord, coding, parse_dt

PAPEL_SYSTEM = "http://housia.dev/fhir/CodeSystem/papel-responsavel"
TASK_ID_SYSTEM = "http://housia.dev/fhir/NamingSystem/jornada-tarefa"
# o que vira Task.input: só recursos FHIR de verdade (não o checklist, a avaliação ou as falas do HousIA)
FHIR_TYPES = {
    "Encounter", "Appointment", "Condition", "MedicationRequest", "Observation", "DocumentReference", "Patient",
}  # fmt: skip
CLOSING = {"concluida": "completed", "cancelada": "cancelled"}


# ---------- fatos do banco ----------
def load_facts(db: Session, now: datetime) -> Facts:
    facts = Facts()
    for a in db.scalars(select(DischargeAssessment).order_by(DischargeAssessment.id.desc())):
        facts.assessments.setdefault(a.patient_id, []).append(a)
    for c in db.scalars(select(JourneyContact).order_by(JourneyContact.id)):
        facts.contacts.setdefault(c.patient_id, []).append(c)
    for pid, when in db.execute(select(MedrecRun.patient_id, MedrecRun.created_at)):
        facts.medrec.setdefault(pid, []).append(when)
    window = now - timedelta(days=reference.params()["adesao_janela_dias"] + 1)
    for c in db.scalars(
        select(Consultation)
        .where(
            Consultation.status.in_(("finalizada", "enviada")),
            Consultation.created_at >= window,
            Consultation.sandbox.is_(False),  # sandbox é teste, não gera acompanhamento (#34)
        )
        .order_by(Consultation.id.desc())
    ):
        facts.consultations.setdefault(c.patient_id, []).append(c)
    for t in db.scalars(select(JourneyTask).where(JourneyTask.status == "aberta")):
        facts.tasks[(t.patient_id, t.rule)] = t
    return facts


# ---------- lista ----------
def patient_card(record: PatientRecord) -> dict:
    phone = next((t["value"] for t in record.patient.get("telecom", []) if t.get("value")), None)
    relative = None
    for rp in record.related_persons:
        name = (rp.get("name") or [{}])[0].get("text")
        if name:
            rel = ((rp.get("relationship") or [{}])[0].get("coding") or [{}])[0].get("display") or ""
            relative = {
                "nome": name,
                "parentesco": rel,
                "telefone": next((t["value"] for t in rp.get("telecom", []) if t.get("value")), None),
            }
            break
    return {"id": record.id, "nome": record.name, "idade": record.age, "sexo": record.sex, "telefone": phone,
            "familiar": relative}  # fmt: skip


def task_out(t: JourneyTask) -> dict:
    return {
        "id": t.id,
        "patient_id": t.patient_id,
        "regra": t.rule,
        "titulo": t.title,
        "detalhe": t.detail,
        "papel": t.role,
        "prazo": t.due_at.isoformat(),
        "status": t.status,
        "fontes": t.source_refs,
        "fhir_ref": t.fhir_ref,
        "criada_em": t.created_at.isoformat(),
        "encerrada_em": t.closed_at.isoformat() if t.closed_at else None,
        "observacao_encerramento": t.closing_note,
    }


def item_out(item: Item, record: PatientRecord, facts: Facts, now: datetime) -> dict:
    meta = reference.rule_meta(item.rule)
    task = facts.tasks.get((item.patient_id, item.rule))
    return {
        "id": f"{item.rule}:{item.patient_id}",
        "regra": item.rule,
        "regra_titulo": meta["titulo"],
        "severidade": item.severity,
        "situacao": item.situacao,
        "paciente": patient_card(record),
        "titulo": item.titulo,
        "detalhe": item.detalhe,
        "fontes": item.refs,
        "fonte_regra": " ".join(meta["fonte"].split()),
        "desde": item.desde.isoformat(),
        "prazo": item.prazo.isoformat() if item.prazo else None,
        "papel_sugerido": meta["papel"],
        "prazo_tarefa_horas": int(meta["prazo_horas"]),
        "tarefa": task_out(task) if task else None,
        "contexto": item.contexto,
    }


def _sort_key(row: dict):
    return (SEVERITY_RANK[row["severidade"]], row["tarefa"] is not None, row["desde"])


def worklist(
    db: Session,
    fhir: FhirSource,
    *,
    patient_id: str | None = None,
    rule: str | None = None,
    role: str | None = None,
    severity: str | None = None,
) -> dict:
    now = clock.now()
    facts = load_facts(db, now)
    if patient_id:
        patient_ids = [load_record(fhir, patient_id).id]
    else:
        confirmed = {pid for pid, rows in facts.assessments.items() if any(a.confirmed_at for a in rows)}
        admitted = {
            (e.get("subject") or {}).get("reference", "").split("/")[-1]
            for e in fhir.active_encounters()
            if not e.get("partOf") and e.get("class", {}).get("code") == "IMP"
        }
        org = current_org(db)
        patient_ids = [
            p["id"]
            for p in fhir.list_patients()
            if (p["id"] not in admitted or p["id"] in confirmed) and avulso.visible(p["id"], org)
        ]
    rows: list[dict] = []
    for pid in patient_ids:
        record = load_record(fhir, pid)
        rows += [item_out(i, record, facts, now) for i in evaluate(record, facts, now)]
    if rule:
        rows = [r for r in rows if r["regra"] == rule]
    if role:
        rows = [r for r in rows if r["papel_sugerido"] == role]
    if severity:
        rows = [r for r in rows if r["severidade"] == severity]
    rows.sort(key=_sort_key)
    summary = {
        "itens": len(rows),
        "pacientes": len({r["paciente"]["id"] for r in rows}),
        "criticos": sum(r["severidade"] == "critico" for r in rows),
        "com_tarefa": sum(r["tarefa"] is not None for r in rows),
        "por_regra": {k: sum(r["regra"] == k for r in rows) for k in reference.config()["regras"]},
    }
    return {"gerado_em": now.isoformat(), "resumo": summary, "itens": rows}


# ---------- tarefas ----------
def _task_resource(task: JourneyTask, item: Item, user: User) -> dict:
    inputs = [
        {"type": {"text": "Fonte"}, "valueReference": {"reference": ref}}
        for ref in item.refs
        if ref.split("/")[0] in FHIR_TYPES
    ]
    resource = {
        "resourceType": "Task",
        "identifier": [{"system": TASK_ID_SYSTEM, "value": str(task.id)}],
        "status": "requested",
        "intent": "order",
        "priority": "urgent" if item.severity == "critico" else "routine",
        "code": {"text": task.title},
        "description": task.detail,
        "for": {"reference": f"Patient/{task.patient_id}"},
        "authoredOn": task.created_at.isoformat(),
        "requester": {"display": user.name},
        "performerType": [
            {
                "coding": [{"system": PAPEL_SYSTEM, "code": task.role, "display": reference.PAPEL_LABEL[task.role]}],
                "text": reference.PAPEL_LABEL[task.role],
            }
        ],
        "restriction": {"period": {"end": task.due_at.isoformat()}},
    }
    if inputs:
        resource["input"] = inputs
    return resource


def create_task(
    db: Session,
    fhir: FhirSource,
    user: User,
    patient_id: str,
    rule: str,
    role: str | None = None,
    hours: int | None = None,
) -> dict:
    """Cria a tarefa a partir do item que está na lista agora (o texto e as fontes vêm do servidor, não do cliente)."""
    if rule not in reference.config()["regras"]:
        raise HTTPException(422, "Regra desconhecida.")
    now = clock.now()
    facts = load_facts(db, now)
    record = load_record(fhir, patient_id)
    item = next((i for i in evaluate(record, facts, now) if i.rule == rule), None)
    if item is None:
        raise HTTPException(409, "Este paciente não está mais na lista para essa regra.")
    if (patient_id, rule) in facts.tasks:
        raise HTTPException(409, f"Já existe a tarefa #{facts.tasks[(patient_id, rule)].id} aberta para este item.")
    meta = reference.rule_meta(rule)
    task = JourneyTask(
        patient_id=patient_id,
        rule=rule,
        title=item.titulo[:300],
        detail=item.detalhe,
        role=role or meta["papel"],
        due_at=now + timedelta(hours=hours or int(meta["prazo_horas"])),
        status="aberta",
        source_refs=item.refs,
        created_by=user.id,
        created_at=now,
    )
    db.add(task)
    db.flush()
    created = fhir.create(_task_resource(task, item, user))
    task.fhir_ref = f"Task/{created['id']}"
    log_action(
        db, user, "jornada.tarefa_criada", "journey_task", task.id,
        {"patient_id": patient_id, "regra": rule, "papel": task.role, "prazo": task.due_at.isoformat(),
         "fhir": task.fhir_ref},
        commit=False,
    )  # fmt: skip
    db.commit()
    return task_out(task)


def _close(db: Session, fhir: FhirSource, task: JourneyTask, user: User | None, status: str, note: str | None) -> None:
    task.status, task.closed_by, task.closed_at, task.closing_note = (
        status,
        user.id if user else None,
        clock.now(),
        note,
    )
    if task.fhir_ref:
        resource = fhir.get("Task", task.fhir_ref.split("/", 1)[1])
        if resource is not None:
            fhir.update(resource | {"status": CLOSING[status]})


def update_task(db: Session, fhir: FhirSource, user: User, task_id: int, status: str, note: str | None) -> dict:
    task = db.get(JourneyTask, task_id)
    if task is None:
        raise HTTPException(404, "Tarefa não encontrada")
    if task.status != "aberta":
        raise HTTPException(409, "A tarefa já está encerrada.")
    if status == "cancelada" and len((note or "").strip()) < 10:
        raise HTTPException(422, "Para cancelar a tarefa, registre o motivo (mín. 10 caracteres).")
    _close(db, fhir, task, user, status, (note or "").strip() or None)
    log_action(
        db, user, "jornada.tarefa_" + status, "journey_task", task.id,
        {"patient_id": task.patient_id, "regra": task.rule}, commit=False,
    )  # fmt: skip
    db.commit()
    return task_out(task)


def list_tasks(db: Session, fhir: FhirSource, status: str | None, role: str | None) -> list[dict]:
    query = select(JourneyTask).order_by(JourneyTask.due_at)
    if status:
        query = query.where(JourneyTask.status == status)
    if role:
        query = query.where(JourneyTask.role == role)
    out = []
    names: dict[str, str] = {}
    for t in db.scalars(query):
        if t.patient_id not in names:
            patient = fhir.get("Patient", t.patient_id)
            n = ((patient or {}).get("name") or [{}])[0]
            names[t.patient_id] = n.get("text") or t.patient_id
        out.append(task_out(t) | {"paciente": {"id": t.patient_id, "nome": names[t.patient_id]}})
    return out


# ---------- contato pós-alta ----------
def contact_script(record: PatientRecord) -> list[dict]:
    """Perguntas do roteiro para este paciente: as gerais e as das condições e medicamentos dele."""
    cids = {
        icd["code"].replace(".", "").upper()
        for c in record.active_conditions
        if (icd := coding(c.get("code"), codes.ICD10))
    }
    meds = {m.ingredient for m in record.medications if m.status in ("active", "completed")}
    out = []
    for q in reference.script():
        when = q.get("quando") or {}
        if "cid" in when and not any(c.startswith(p.replace(".", "").upper()) for c in cids for p in when["cid"]):
            continue
        if "medicamento" in when and not meds & set(when["medicamento"]):
            continue
        out.append(
            {"id": q["id"], "tipo": q["tipo"], "texto": " ".join(q["texto"].split()), "alarme": q.get("alarme"),
             "risco_quando": "sim" if q["tipo"] == "sinal_alarme" else q.get("risco_quando", "nao")}
        )  # fmt: skip
    return out


def _contact_text(
    record: PatientRecord,
    user: User,
    channel: str,
    questions: list[dict],
    answers: dict,
    notes: str | None,
    when: datetime,
) -> str:
    label = {"sim": "sim", "nao": "não", "nao_se_aplica": "não se aplica"}
    lines = [f"Contato pós-alta ({channel}) em {local_day(when)} por {user.name}.", ""]
    for q in questions:
        if q["id"] in answers:
            lines.append(f"- {q['texto']} {label[answers[q['id']]].capitalize()}.")
    alarms = [q["alarme"] for q in questions if q["tipo"] == "sinal_alarme" and answers.get(q["id"]) == "sim"]
    if alarms:
        lines += ["", "SINAIS DE ALARME RELATADOS: " + "; ".join(alarms) + ". Alerta crítico aberto no HousIA."]
    if notes:
        lines += ["", f"Observações: {notes}"]
    lines += [
        "",
        "Registro do Waze da jornada (HousIA). O contato foi feito pela equipe; o HousIA não envia mensagens.",
    ]
    return "\n".join(lines)


def register_contact(db: Session, fhir: FhirSource, user: User, patient_id: str, body) -> dict:
    now = clock.now()
    record = load_record(fhir, patient_id)
    facts = load_facts(db, now)
    admitted, discharge = discharge_of(record, facts, now)
    if admitted:
        raise HTTPException(409, "Paciente ainda internado: o contato pós-alta começa depois da alta.")
    questions = contact_script(record)
    by_id = {q["id"]: q for q in questions}
    answers = {k: v for k, v in (body.respostas or {}).items()}
    unknown = [k for k in answers if k not in by_id]
    if unknown:
        raise HTTPException(422, f"Pergunta(s) fora do roteiro deste paciente: {', '.join(sorted(unknown))}")
    bad = [k for k, v in answers.items() if v not in reference.ANSWERS]
    if bad:
        raise HTTPException(422, "Respostas aceitas: sim, nao, nao_se_aplica.")
    if body.atendeu:
        missing = [q["id"] for q in questions if q["tipo"] == "sinal_alarme" and q["id"] not in answers]
        if missing:
            raise HTTPException(
                422, "Todos os sinais de alarme precisam ser perguntados: faltam " + ", ".join(missing) + "."
            )
    else:
        answers = {}
    notes = (body.observacoes or "").strip()[:1000] or None
    alarms = [q for q in questions if q["tipo"] == "sinal_alarme" and answers.get(q["id"]) == "sim"]
    pendencies = [
        {"pergunta": q["id"], "tipo": q["tipo"], "texto": q["texto"]}
        for q in questions
        if q["tipo"] != "sinal_alarme" and answers.get(q["id"]) == q["risco_quando"]
    ]
    encounter_id = discharge.encounter_id if discharge else (record.hospitalization or {}).get("id")
    contact = JourneyContact(
        patient_id=patient_id,
        encounter_id=encounter_id,
        channel=body.canal,
        reached=body.atendeu,
        answers_json=answers,
        alarm_signs_json=[q["id"] for q in alarms],
        pendencies_json=pendencies,
        notes=notes,
        created_by=user.id,
        created_at=now,
    )
    db.add(contact)
    db.flush()
    document = alert = text = None
    if body.atendeu:
        text = _contact_text(record, user, body.canal, questions, answers, notes, now)
        document = fhir.create(_document(record, user, now, text, encounter_id, contact.id))
        contact.fhir_ref = f"DocumentReference/{document['id']}"
        if alarms:
            labels = ", ".join(q["alarme"].lower() for q in alarms)
            refs = [contact.fhir_ref] + ([f"Encounter/{encounter_id}"] if encounter_id else [])
            alert = upsert_alert(
                db,
                patient_id=patient_id,
                source="jornada",
                severity="critico",
                title=f"Sinal de alarme no contato pós-alta: {labels}"[:300],
                detail=f"Relatado em {local_day(now)} por {user.name} ({body.canal}). Avaliar o paciente hoje.",
                refs=refs,
            )
            contact.alert_id = alert.id
        for task in list(facts.tasks.values()):  # o contato resolve as tarefas de contato abertas do paciente
            if task.patient_id == patient_id and task.rule in (
                "alta_sem_contato_72h",
                "falta_ao_retorno",
                "nao_adesao_consulta",
            ):
                _close(db, fhir, task, user, "concluida", f"Encerrada pelo contato pós-alta #{contact.id}.")
    log_action(
        db, user, "jornada.contato_registrado", "journey_contact", contact.id,
        {"patient_id": patient_id, "canal": body.canal, "atendeu": body.atendeu, "alarmes": contact.alarm_signs_json,
         "pendencias": [p["pergunta"] for p in pendencies], "fhir": contact.fhir_ref,
         "alerta": alert.id if alert else None},
        commit=False,
    )  # fmt: skip
    db.commit()
    return contact_out(contact) | {
        "alerta": {"id": alert.id, "titulo": alert.title, "severidade": alert.severity} if alert else None,
        "texto": text,
    }


def contact_out(c: JourneyContact) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "encounter_id": c.encounter_id,
        "canal": c.channel,
        "atendeu": c.reached,
        "respostas": c.answers_json,
        "sinais_de_alarme": c.alarm_signs_json,
        "pendencias": c.pendencies_json,
        "observacoes": c.notes,
        "fhir_ref": c.fhir_ref,
        "alert_id": c.alert_id,
        "registrado_em": c.created_at.isoformat(),
    }


def _document(record: PatientRecord, user: User, when: datetime, text: str, encounter_id: str | None, cid: int) -> dict:
    resource = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final",
        "identifier": [{"system": "http://housia.dev/fhir/NamingSystem/jornada-contato", "value": str(cid)}],
        "type": {
            "coding": [{"system": codes.LOINC, "code": codes.NOTE_NURSING, "display": "Nurse note"}],
            "text": "Contato pós-alta (HousIA)",
        },
        "subject": {"reference": f"Patient/{record.id}", "display": record.name},
        "date": when.isoformat(),
        "author": [{"display": user.name}],
        "description": "Contato pós-alta (HousIA)",
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain; charset=utf-8",
                    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "title": "Contato pós-alta",
                }
            }
        ],
    }
    if encounter_id:
        resource["context"] = {"encounter": [{"reference": f"Encounter/{encounter_id}"}]}
    return resource


def patient_contacts(db: Session, patient_id: str) -> list[dict]:
    rows = db.scalars(
        select(JourneyContact).where(JourneyContact.patient_id == patient_id).order_by(JourneyContact.id.desc())
    )
    return [contact_out(c) for c in rows]


# ---------- texto pronto (sem envio) ----------
def message_draft(db: Session, fhir: FhirSource, patient_id: str) -> dict:
    """Mensagem de acompanhamento pronta para a equipe copiar e enviar pelo canal oficial. O HousIA não envia."""
    now = clock.now()
    record = load_record(fhir, patient_id)
    facts = load_facts(db, now)
    _admitted, discharge = discharge_of(record, facts, now)
    first = (record.name.split() or ["paciente"])[0]
    lines = [f"Olá, {first}! Aqui é a equipe do hospital."]
    if discharge is not None:
        lines.append(f"Estamos passando para saber como você está desde a alta, em {local_day(discharge.at)}.")
    else:
        lines.append("Estamos passando para saber como você está.")
    meds = _home_meds(record, discharge.at if discharge else None)
    if meds:
        lines += ["", "Seus remédios (confira com a sua receita):"] + [f"- {m}" for m in meds]
    follow = [
        a for a in record.resources.get("Appointment", [])
        if a.get("status") in ("booked", "pending") and (s := parse_dt(a.get("start"))) and s > now
        and not any((b.get("reference") or "").startswith("ServiceRequest/") for b in a.get("basedOn", []))
    ]  # fmt: skip
    lines.append("")
    if follow:
        nxt = min(follow, key=lambda a: a["start"])
        lines.append(
            f"Seu retorno está marcado para {local_day(parse_dt(nxt['start']))}: {nxt.get('description', '')}."
        )  # type: ignore[arg-type]
    else:
        lines.append("Ainda precisamos confirmar a data do seu retorno. Responda esta mensagem ou ligue para nós.")
    alarms = [q["alarme"] for q in contact_script(record) if q["tipo"] == "sinal_alarme"]
    lines += ["", "Procure o pronto-socorro ou ligue 192 (SAMU) se tiver:"] + [f"- {a.lower()}" for a in alarms]
    lines += ["", "Qualquer dúvida sobre os remédios ou o retorno, responda esta mensagem. Cuide-se!"]
    return {
        "texto": "\n".join(lines),
        "envio": False,
        "aviso": "Texto gerado para a equipe revisar. O HousIA não envia SMS nem WhatsApp: copie e envie pelo canal "
        "oficial do hospital, ou use como roteiro de ligação.",
        "telefone": next((t["value"] for t in record.patient.get("telecom", []) if t.get("value")), None),
        "fontes": discharge.refs if discharge else [],
    }


def _home_meds(record: PatientRecord, discharge_at: datetime | None) -> list[str]:
    seen: dict[str, str] = {}
    for m in sorted(record.medications, key=lambda m: m.start or datetime.min.replace(tzinfo=clock.now().tzinfo)):
        if m.status == "stopped" or m.prn or m.route.lower() not in HOME_ROUTES:
            continue
        if m.status == "completed" and (
            discharge_at is None or m.end is None or m.end < discharge_at - timedelta(hours=6)
        ):
            continue
        seen[m.ingredient] = m.text
    return list(seen.values())
