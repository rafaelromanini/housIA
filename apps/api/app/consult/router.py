from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_roles
from app.config import get_settings
from app.consult import knowledge, service, stt
from app.consult.schemas import LANGUAGE_LABELS, ConsultCreate, SegmentsIn, Speaker
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir

router = APIRouter(tags=["consulta com IA"])
clinician = require_roles("medico", "enfermagem")


@router.get("/consult/config")
def config(_: User = Depends(get_current_user)) -> dict:
    """O que está disponível neste ambiente (para a tela montar os controles)."""
    s = get_settings()
    return {
        "llm_mode": s.llm_mode,
        "stt_servidor": stt.enabled(),
        "envio_prontuario": s.ehr_writeback,
        "idiomas": [{"value": k, "label": v} for k, v in LANGUAGE_LABELS.items()],
        "especialidades": [
            {"value": k, "label": v, "foco": knowledge.SPECIALTY_FOCUS.get(k, "")}
            for k, v in knowledge.SPECIALTIES.items()
        ],
    }


@router.get("/appointments")
def appointments(
    db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), _: User = Depends(clinician)
) -> list[dict]:
    """Agenda ambulatorial do dia (Encounter class AMB no FHIR) com o status da consulta no HousIA."""
    return service.appointments(db, fhir)


@router.get("/consult/demo-scripts/{script_id}")
def demo_script(script_id: str, _: User = Depends(clinician)) -> dict:
    """Roteiro sintético para simular a conversa (sem microfone), usado na demo e nos testes."""
    script = knowledge.demo_scripts().get(script_id)
    if script is None:
        raise HTTPException(404, "Roteiro não encontrado")
    return script


@router.post("/patients/{patient_id}/consultations")
def create_consultation(
    patient_id: str,
    body: ConsultCreate,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    return service.create(db, fhir, user, patient_id, body)


@router.get("/patients/{patient_id}/consultations")
def list_consultations(patient_id: str, db: Session = Depends(get_db), _: User = Depends(clinician)) -> list[dict]:
    return service.list_for_patient(db, patient_id)


@router.get("/consultations/{cid}")
def get_consultation(cid: int, db: Session = Depends(get_db), user: User = Depends(clinician)) -> dict:
    return service.get(db, user, cid)


@router.post("/consultations/{cid}/segments")
def add_segments(
    cid: int,
    body: SegmentsIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Recebe falas transcritas e devolve os Smart Apps atualizados na hora (camada de regras)."""
    return service.append_segments(db, fhir, user, cid, body.segmentos)


@router.post("/consultations/{cid}/audio")
async def add_audio(
    cid: int,
    request: Request,
    falante: Speaker = Query("desconhecido"),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Trecho de áudio (corpo bruto, ex.: audio/webm) → transcrição no servidor (STT_URL) → falas."""
    audio = await request.body()
    return service.transcribe_audio(db, fhir, user, cid, audio, request.headers.get("content-type", ""), falante)


@router.post("/consultations/{cid}/insights")
def run_insights(
    cid: int,
    force: bool = False,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Ciclo da IA sobre a conversa até aqui (Claude em LLM_MODE=real)."""
    return service.analyze(db, fhir, user, cid, force)


@router.post("/consultations/{cid}/translate")
def translate(
    cid: int,
    ns: list[int] | None = Body(None, embed=True),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    return service.translate(db, fhir, user, cid, ns)


@router.post("/consultations/{cid}/finish")
def finish(
    cid: int, db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), user: User = Depends(clinician)
) -> dict:
    """Encerra a captura e gera a nota (ou o relatório cirúrgico) para revisão."""
    return service.finish(db, fhir, user, cid)


@router.put("/consultations/{cid}/note")
def update_note(
    cid: int, note: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(clinician)
) -> dict:
    """Revisão/edição da nota pelo profissional (obrigatória antes do envio)."""
    return service.update_note(db, user, cid, note)


@router.post("/consultations/{cid}/send")
def send(
    cid: int, db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), user: User = Depends(clinician)
) -> dict:
    """Envia a nota aprovada ao prontuário como FHIR DocumentReference (ou devolve o recurso para exportar)."""
    return service.send_to_ehr(db, fhir, user, cid)
