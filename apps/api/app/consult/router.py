from datetime import date
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_roles
from app.config import get_settings
from app.consult import antiglosa, knowledge, opme, service, stt
from app.consult import ask as copilot
from app.consult.schemas import LANGUAGE_LABELS, ConsultCreate, SegmentsIn, Speaker
from app.consult.templates import surgical_specialties
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir

router = APIRouter(tags=["consulta com IA"])
clinician = require_roles("medico", "enfermagem")


@router.get("/consult/config")
def config(_: User = Depends(get_current_user)) -> dict:
    """O que está disponível neste ambiente (para a tela montar os controles)."""
    s = get_settings()
    surgical = set(surgical_specialties())
    return {
        "llm_mode": s.llm_mode,
        "stt_servidor": stt.enabled(),
        "envio_prontuario": s.ehr_writeback,
        "idiomas": [{"value": k, "label": v} for k, v in LANGUAGE_LABELS.items()],
        "especialidades": [
            {"value": k, "label": v, "foco": knowledge.SPECIALTY_FOCUS.get(k, ""), "cirurgica": k in surgical}
            for k, v in knowledge.SPECIALTIES.items()
        ],
    }


@router.get("/consult/opme-catalog")
def opme_catalog(q: str = Query("", max_length=80), _: User = Depends(clinician)) -> list[dict]:
    """Autocomplete do catálogo de OPME (demonstração: códigos DEMO-OPME-*) por código, descrição ou nome dito."""
    return opme.search(q)


@router.get("/consult/convenios")
def convenios(_: User = Depends(clinician)) -> list[dict]:
    """Convênios com regras anti-glosa em dados (data/reference/glosa_rules)."""
    return antiglosa.convenio_list()


@router.get("/appointments")
def appointments(
    db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), _: User = Depends(clinician)
) -> list[dict]:
    """Agenda ambulatorial do dia (Encounter class AMB no FHIR) com o status da consulta no HousIA."""
    return service.appointments(db, fhir)


@router.get("/consult/demo-scripts")
def demo_scripts(_: User = Depends(clinician)) -> list[dict]:
    """Roteiros sintéticos disponíveis (consulta, visita e centro cirúrgico), sem as falas."""
    return [
        {
            "id": s["id"],
            "patient_id": s.get("patient_id"),
            "tipo": s.get("tipo"),
            "especialidade": s.get("especialidade"),
            "titulo": s.get("titulo"),
            "falas": len(s.get("falas", [])),
        }
        for s in knowledge.demo_scripts().values()
    ]


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


class ChecklistIn(BaseModel):
    etapa: Literal["sign_in", "time_out", "sign_out"]
    feito: bool = True


@router.post("/consultations/{cid}/checklist")
def checklist(
    cid: int,
    body: ChecklistIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Cirurgia Segura (OMS) por botão: sign-in, time-out ou sign-out (o ditado também marca, por voz)."""
    return service.mark_checklist(db, fhir, user, cid, body.etapa, body.feito)


class MainComplaintIn(BaseModel):
    chave: str | None = None  # chave do sintoma (ex.: "tontura"); vazio solta a queixa fixada


@router.put("/consultations/{cid}/queixa-principal")
def main_complaint(
    cid: int,
    body: MainComplaintIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Fixa a queixa principal da anamnese (por padrão é a primeira dita pelo paciente)."""
    return service.set_main_complaint(db, fhir, user, cid, body.chave)


class PedidoDecisaoIn(BaseModel):
    """Assinatura item a item: assinar, recusar ou voltar a pendente. Item crítico exige justificativa."""

    estado: Literal["assinado", "recusado", "pendente"]
    justificativa: str | None = Field(None, max_length=1000)
    # posologia corrigida pelo profissional antes de assinar (só os campos enviados mudam)
    dose: str | None = Field(None, max_length=60)
    via: str | None = Field(None, max_length=40)
    frequencia: str | None = Field(None, max_length=80)
    duracao: str | None = Field(None, max_length=60)


@router.put("/consultations/{cid}/pedidos/{draft_id}")
def decide_order(
    cid: int,
    draft_id: str,
    body: PedidoDecisaoIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Assina ou recusa um rascunho de receita/pedido de exame (item crítico só com justificativa, auditada)."""
    return service.decide_order(db, fhir, user, cid, draft_id, body)


class HypothesisDecisionIn(BaseModel):
    cid: str = Field(..., min_length=3, max_length=10)
    decisao: Literal["pendente", "confirmada", "provisoria", "descartada"]
    motivo: str = Field("", max_length=300)


@router.put("/consultations/{cid}/hipoteses")
def hypothesis_decision(
    cid: int,
    body: HypothesisDecisionIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Confirma, deixa provisória ou descarta uma hipótese (gera proposta de Condition ou negativa pertinente)."""
    return service.set_hypothesis_decision(db, fhir, user, cid, body.cid, body.decisao, body.motivo)


class OpmeItemIn(BaseModel):
    codigo: str = Field(..., min_length=1, max_length=40)
    quantidade: int = Field(1, ge=1, le=99)
    descricao: str | None = Field(None, max_length=200)
    palavras: list[str] | None = Field(None, max_length=10)
    gtin: str | None = Field(None, max_length=14)


class OpmeGuiaIn(BaseModel):
    """Guia autorizada: itens manuais (autocomplete do catálogo), ClaimResponse FHIR ou XML TISS — exatamente um."""

    itens: list[OpmeItemIn] | None = Field(None, max_length=60)
    claim_response: dict | None = None
    tiss_xml: str | None = Field(None, max_length=1_000_000)
    numero: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _one_source(self):
        if sum(x is not None for x in (self.itens, self.claim_response, self.tiss_xml)) != 1:
            raise ValueError("Informe exatamente uma origem: itens, claim_response ou tiss_xml")
        return self


@router.put("/consultations/{cid}/opme-autorizado")
def set_opme_guia(
    cid: int,
    body: OpmeGuiaIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Guia de OPME autorizada (entrada manual, ClaimResponse ou XML TISS): acaba a dependência do roteiro de demo."""
    return service.set_opme_guia(db, fhir, user, cid, body)


class OpmeConsumoIn(BaseModel):
    """Leitura da etiqueta GS1 (DataMatrix/GS1-128) feita pela câmera do tablet, ou código com lote/série digitados."""

    etiqueta: str | None = Field(None, max_length=400)
    codigo: str | None = Field(None, max_length=40)
    quantidade: int = Field(1, ge=1, le=99)
    lote: str | None = Field(None, max_length=40)
    serie: str | None = Field(None, max_length=40)
    validade: date | None = None


@router.post("/consultations/{cid}/opme-consumo")
def add_opme_consumo(
    cid: int,
    body: OpmeConsumoIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Registra o consumo de um item (etiqueta interpretada no servidor) e reconcilia com a fala e a guia."""
    return service.add_opme_consumo(db, fhir, user, cid, body)


@router.delete("/consultations/{cid}/opme-consumo/{item_id}")
def remove_opme_consumo(
    cid: int,
    item_id: str,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    return service.remove_opme_consumo(db, fhir, user, cid, item_id)


class FaturamentoIn(BaseModel):
    convenio: str | None = Field(None, max_length=40)
    numero_guia: str | None = Field(None, max_length=60)


@router.put("/consultations/{cid}/faturamento")
def set_faturamento(
    cid: int,
    body: FaturamentoIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Convênio e número da guia das regras anti-glosa (a prontidão para faturamento é recalculada)."""
    return service.set_faturamento(db, fhir, user, cid, body.convenio, body.numero_guia)


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


class SendIn(BaseModel):
    justificativa: str | None = Field(None, max_length=1000)


@router.post("/consultations/{cid}/send")
def send(
    cid: int,
    body: SendIn | None = Body(None),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Envia a nota aprovada ao prontuário como FHIR DocumentReference (ou devolve o recurso para exportar).

    Na cirurgia, pendência crítica de faturamento devolve 409 até haver uma justificativa (auditada).
    """
    return service.send_to_ehr(db, fhir, user, cid, body.justificativa if body else None)


@router.get("/consultations/{cid}/export")
def export(
    cid: int, db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), user: User = Depends(clinician)
) -> dict:
    """Texto pronto para o prontuário e o recurso FHIR (DocumentReference), sem enviar."""
    return service.export(db, fhir, user, cid)


class AskIn(BaseModel):
    pergunta: str = Field(min_length=1, max_length=copilot.MAX_QUESTION)
    via: Literal["texto", "voz"] = "texto"  # voz (Alt+Q) vira pergunta ao copiloto, nunca uma fala da consulta


class InsertIn(BaseModel):
    mensagem_id: int
    destino: str = "avaliacao"
    frases: list[int] | None = None  # índices das frases (padrão: todas)


@router.get("/consultations/{cid}/copiloto")
def copilot_state(
    cid: int, db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), user: User = Depends(clinician)
) -> dict:
    """Histórico do copiloto da consulta, inserções na nota e sugestões de discussão."""
    return service.copilot_state(db, fhir, user, cid)


@router.post("/consultations/{cid}/ask")
def ask(
    cid: int,
    body: AskIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Pergunta livre ao prontuário e à conversa: regras primeiro, IA no que sobrar, fonte em cada frase."""
    return service.ask(db, fhir, user, cid, body.pergunta, body.via)


@router.post("/consultations/{cid}/copiloto/inserir")
def copilot_insert(
    cid: int,
    body: InsertIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Insere na nota (com as refs, origem "copiloto") as frases de uma resposta do copiloto."""
    return service.insert_into_note(db, fhir, user, cid, body.mensagem_id, body.destino, body.frases)
