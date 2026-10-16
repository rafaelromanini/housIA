from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.journey import metrics, reference, service
from app.journey.rules import RULES
from app.patients.service import load_record

router = APIRouter(prefix="/journey", tags=["jornada do paciente"])

Role = Literal["medico", "enfermagem", "gestor"]


class TaskIn(BaseModel):
    patient_id: str
    regra: str
    papel: Role | None = None
    prazo_horas: int | None = Field(None, ge=1, le=24 * 30)


class TaskUpdateIn(BaseModel):
    status: Literal["concluida", "cancelada"]
    observacao: str | None = Field(None, max_length=1000)


class ContactIn(BaseModel):
    canal: Literal["telefone", "visita", "presencial", "mensagem"] = "telefone"
    atendeu: bool = Field(description="Falou com o paciente ou cuidador. Tentativa sem sucesso: false.")
    respostas: dict[str, str] = Field(default_factory=dict)
    observacoes: str | None = Field(None, max_length=1000)


@router.get("/worklist")
def get_worklist(
    regra: str | None = Query(None, description="id da regra"),
    papel: Role | None = None,
    severidade: Literal["critico", "atencao", "info"] | None = None,
    patient_id: str | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> dict:
    """Pacientes em risco de abandono: alta de alto risco sem contato, retorno não agendado, falta, protocolo de APS
    vencido, sinal de não adesão e troca de medicamento sem reconciliação. Cada item cita as fontes."""
    if regra and regra not in RULES:
        raise HTTPException(422, f"Regra desconhecida. Use uma de: {', '.join(RULES)}")
    return service.worklist(db, fhir, patient_id=patient_id, rule=regra, role=papel, severity=severidade)


@router.get("/rules")
def get_rules(_: User = Depends(get_current_user)) -> dict:
    cfg = reference.config()
    return {
        "parametros": cfg["parametros"],
        "regras": [
            {"id": k, "titulo": v["titulo"], "papel": v["papel"], "prazo_horas": v["prazo_horas"],
             "fonte": " ".join(v["fonte"].split())}
            for k, v in cfg["regras"].items()
        ],
    }  # fmt: skip


@router.get("/metrics")
def get_metrics(
    coorte: Literal["risco", "mes", "cid"] = "risco",
    dias: int = Query(365, ge=30, le=3650),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> dict:
    """Reinternação em 30 dias e retornos perdidos por coorte, com os casos (Encounter IMP) que sustentam a conta."""
    return metrics.compute(fhir, coorte, dias)


@router.get("/tasks")
def get_tasks(
    status: Literal["aberta", "concluida", "cancelada"] | None = None,
    papel: Role | None = None,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> list[dict]:
    return service.list_tasks(db, fhir, status, papel)


@router.post("/tasks", status_code=201)
def post_task(
    body: TaskIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Cria a tarefa (FHIR Task com responsável por papel e prazo) a partir de um item da lista."""
    return service.create_task(db, fhir, user, body.patient_id, body.regra, body.papel, body.prazo_horas)


@router.patch("/tasks/{task_id}")
def patch_task(
    task_id: int,
    body: TaskUpdateIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    return service.update_task(db, fhir, user, task_id, body.status, body.observacao)


@router.get("/patients/{patient_id}/contact-script")
def get_contact_script(
    patient_id: str, fhir: FhirSource = Depends(get_fhir), _: User = Depends(get_current_user)
) -> dict:
    """Roteiro do contato pós-alta (teach-back, retirada dos remédios e sinais de alarme do paciente)."""
    return {"perguntas": service.contact_script(load_record(fhir, patient_id))}


@router.get("/patients/{patient_id}/message-draft")
def get_message_draft(
    patient_id: str,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> dict:
    """Texto pronto de acompanhamento. Sem envio: a equipe copia e usa o canal oficial."""
    return service.message_draft(db, fhir, patient_id)


@router.get("/patients/{patient_id}/contacts")
def get_contacts(patient_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> list[dict]:
    return service.patient_contacts(db, patient_id)


@router.post("/patients/{patient_id}/contacts", status_code=201)
def post_contact(
    patient_id: str,
    body: ContactIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Registra o contato feito pela equipe. Sinal de alarme respondido 'sim' abre um alerta crítico; o contato vira
    um DocumentReference no prontuário e tira o paciente da lista de alta sem contato."""
    return service.register_contact(db, fhir, user, patient_id, body)
