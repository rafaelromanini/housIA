from datetime import date
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_roles
from app.config import get_settings
from app.consult import (
    antiglosa,
    cid,
    consentimento,
    consultorio,
    idiomas,
    knowledge,
    note_templates,
    opme,
    perfis,
    privacidade,
    scales,
    service,
    stt,
)
from app.consult import ask as copilot
from app.consult.schemas import ConsultCreate, FinishIn, Marker, SegmentEdit, SegmentsIn, Speaker
from app.consult.templates import surgical_specialties
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.prontidao import ambiente

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
        "stt_navegador": s.allow_browser_stt,
        "privacidade": privacidade.panel(),
        "ambiente": ambiente(s),
        "envio_prontuario": s.ehr_writeback,
        "idiomas": idiomas.opcoes(),
        "aviso_consentimento": consentimento.NOTICES,
        "finalidades": [{"value": k, "label": v} for k, v in consentimento.PURPOSES.items()],
        "especialidades": [
            {
                "value": k,
                "label": v,
                "foco": knowledge.SPECIALTY_FOCUS.get(k, ""),
                "cirurgica": k in surgical,
                "smart_apps": knowledge.specialty_pack(k).smart_apps,
                "escalas": knowledge.specialty_pack(k).escalas,
                "modelo_nota": knowledge.specialty_pack(k).modelo_nota,
            }
            for k, v in knowledge.SPECIALTIES.items()
        ],
    }


class ProfileIn(BaseModel):
    especialidade: str = Field("", max_length=40)
    ocultos: list[str] = Field(default_factory=list, max_length=200)


@router.get("/smart-apps")
def smart_apps(
    especialidade: str = Query("", max_length=40), db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[dict]:
    """Catálogo de Smart Apps: o manifest de cada app e se o perfil do usuário o esconde."""
    return perfis.listing(db, user, especialidade)


@router.get("/smart-apps/perfil")
def smart_apps_profile(
    especialidade: str = Query("", max_length=40), db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Perfil que vale para o usuário nesta especialidade (guardado no servidor, vale em qualquer aparelho)."""
    return perfis.effective(db, user, especialidade)


@router.put("/smart-apps/perfil")
def save_smart_apps_profile(
    body: ProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    return perfis.save(db, user, body.especialidade, body.ocultos)


@router.get("/consult/opme-catalog")
def opme_catalog(q: str = Query("", max_length=80), _: User = Depends(clinician)) -> list[dict]:
    """Autocomplete do catálogo de OPME (demonstração: códigos DEMO-OPME-*) por código, descrição ou nome dito."""
    return opme.search(q)


@router.get("/consult/cid10")
def cid10_search(q: str = Query("", max_length=80), _: User = Depends(clinician)) -> list[dict]:
    """Autocomplete da CID-10 oficial (DATASUS) por código ou descrição, para incluir ou trocar o CID na revisão."""
    return cid.search(q)


@router.get("/consult/convenios")
def convenios(_: User = Depends(clinician)) -> list[dict]:
    """Convênios com regras anti-glosa em dados (data/reference/glosa_rules)."""
    return antiglosa.convenio_list()


@router.get("/consult/plano")
def plano(db: Session = Depends(get_db), _: User = Depends(clinician)) -> dict:
    """Contador do plano da organização: o sandbox não entra na conta."""
    return consultorio.plano(db)


@router.get("/consult/sandbox/cenarios")
def sandbox_scenarios(fhir: FhirSource = Depends(get_fhir), _: User = Depends(clinician)) -> list[dict]:
    """Cenários sintéticos marcados para testar sem gastar o teste grátis (abra a consulta com `sandbox: true`)."""
    return consultorio.scenarios(fhir)


@router.get("/consult/onboarding")
def consultorio_onboarding(db: Session = Depends(get_db), _: User = Depends(clinician)) -> dict:
    """Onboarding de 3 passos do consultório: termina na primeira nota aprovada no sandbox."""
    return consultorio.onboarding(db)


@router.get("/consultations")
def search_consultations(
    status: str | None = Query(
        None, max_length=30, description="em_andamento | finalizada | enviada | aguardando_revisao"
    ),
    q: str = Query("", max_length=80, description="nome ou id do paciente, especialidade, CID"),
    cid: str = Query("", max_length=12, description="código CID-10 (prefixo): E11 devolve E11.9"),
    de: date | None = Query(None, description="criada a partir de (AAAA-MM-DD)"),
    ate: date | None = Query(None, description="criada até (AAAA-MM-DD)"),
    revisada: bool | None = None,
    sandbox: bool | None = None,
    limite: int = Query(50, ge=1, le=200),
    deslocamento: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(clinician),
) -> dict:
    """Busca global de consultas da organização, com a fila de notas finalizadas e ainda não revisadas."""
    return consultorio.search(
        db,
        fhir,
        status=status,
        q=q,
        cid=cid,
        de=de,
        ate=ate,
        revisada=revisada,
        sandbox=sandbox,
        limit=limite,
        offset=deslocamento,
    )


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


@router.get("/patients/{patient_id}/consult/acompanhantes")
def companions(patient_id: str, fhir: FhirSource = Depends(get_fhir), _: User = Depends(clinician)) -> list[dict]:
    """RelatedPerson do paciente (quem pode consentir como acompanhante ou responsável)."""
    return service.companions(fhir, patient_id)


@router.get("/patients/{patient_id}/consult/idioma")
def patient_language(patient_id: str, fhir: FhirSource = Depends(get_fhir), _: User = Depends(clinician)) -> dict:
    """Idioma preferido em Patient.communication, para a tela pré-selecionar o idioma do paciente."""
    return service.idioma_paciente(fhir, patient_id)


@router.post("/patients/{patient_id}/consultations")
def create_consultation(
    patient_id: str,
    body: ConsultCreate,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    return service.create(db, fhir, user, patient_id, body)


@router.delete("/patients/{patient_id}/consultation-data")
def delete_consultation_data(
    patient_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("gestor"))
) -> dict:
    """Exclusão a pedido do titular (LGPD art. 18): só o gestor, auditada."""
    return privacidade.delete_patient_data(db, user, patient_id)


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


@router.patch("/consultations/{cid}/segments/{n}")
def edit_segment(
    cid: int,
    n: int,
    body: SegmentEdit,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Corrige o texto e/ou o falante de uma fala: as regras rodam de novo e a nota existente fica desatualizada."""
    return service.edit_segment(db, fhir, user, cid, n, body)


class MarkerIn(BaseModel):
    tipo: Marker


@router.post("/consultations/{cid}/marker")
def marker(
    cid: int,
    body: MarkerIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Pausa de privacidade, retomada ou revogação do consentimento: vira um marco na transcrição (não é fala)."""
    return service.add_marker(db, fhir, user, cid, body.tipo)


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


class ScaleIn(BaseModel):
    respostas: dict[str, str | int | float] = Field(..., min_length=1, max_length=40)


@router.put("/consultations/{cid}/escalas/{escala}")
def scale_answers(
    cid: int,
    escala: str,
    body: ScaleIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Respostas de uma escala (IPSS, PHQ-9, GAD-7, Morse, Braden, IVCF-20, NYHA ou IMC): o servidor soma os pontos."""
    return service.save_scale(db, fhir, user, cid, escala, body.respostas)


@router.get("/escalas")
def scale_catalog(_: User = Depends(clinician)) -> list[dict]:
    """Escalas disponíveis, com itens, opções e fonte (a tela monta o formulário por aqui)."""
    return scales.catalogo()


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


class InsightActionIn(BaseModel):
    item_key: str = Field(..., min_length=1, max_length=64)
    acao: Literal["aceitar", "descartar", "feita", "nao_se_aplica", "fixar"]
    motivo: str = Field("", max_length=500)


@router.post("/consultations/{cid}/insight-actions")
def insight_action(
    cid: int,
    body: InsightActionIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Aceitar, descartar (motivo ≥ 10 caracteres), marcar como feita / não se aplica ou fixar um insight."""
    return service.apply_insight_action(db, fhir, user, cid, body.item_key, body.acao, body.motivo)


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
    cid: int,
    body: FinishIn | None = Body(None),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Encerra a captura e gera a nota (ou o relatório cirúrgico) para revisão.

    O corpo opcional traz as falas que ainda estavam na fila de captura: entram de forma atômica antes da nota.
    """
    return service.finish(db, fhir, user, cid, body)


@router.put("/consultations/{cid}/note")
def update_note(
    cid: int,
    note: dict = Body(...),
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Revisão/edição da nota pelo profissional (obrigatória antes do envio). Com `"atestacao": true` no corpo, o
    profissional declara "Revisei e assumo a responsabilidade"; sem isso a nota não sai (409 em /send e /export)."""
    return service.update_note(db, user, cid, note, fhir)


@router.get("/consultations/{cid}/note/diff")
def note_diff(cid: int, db: Session = Depends(get_db), user: User = Depends(clinician)) -> dict:
    """Quanto o profissional mudou, seção a seção, em relação ao rascunho que a IA gerou (que fica guardado intacto)."""
    return service.note_diff(db, user, cid)


@router.post("/consultations/{cid}/surgical/recompute")
def recompute_surgical(
    cid: int,
    note: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(clinician),
) -> dict:
    """Recalcula OPME, pendências e prontidão do relatório cirúrgico editado na revisão, sem salvar a nota."""
    return service.recompute_surgical(db, user, cid, note)


class NoteTemplateIn(BaseModel):
    modelo: str = Field(min_length=1, max_length=64)


@router.get("/note-templates")
def note_templates_list(
    especialidade: str | None = Query(None, max_length=64),
    tipo: str | None = Query(None, max_length=32),
    _: User = Depends(clinician),
) -> list[dict]:
    """Modelos de nota versionados (data/reference/note_templates); com especialidade e/ou tipo, marca o sugerido."""
    return note_templates.listing(tipo, especialidade)


@router.put("/consultations/{cid}/note-template")
def switch_note_template(
    cid: int,
    body: NoteTemplateIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(clinician),
) -> dict:
    """Troca o modelo da nota na revisão (o texto já escrito é mantido)."""
    return service.switch_note_template(db, fhir, user, cid, body.modelo)


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


@router.get("/consultations/{cid}/export.pdf")
def export_pdf(
    cid: int, db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), user: User = Depends(clinician)
) -> Response:
    """Nota revisada e atestada em PDF (mesmas travas do /export)."""
    data, filename = service.export_pdf(db, fhir, user, cid)
    return Response(
        data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


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


class IncludeIn(BaseModel):
    n: int = Field(ge=1)
    destino: str = "avaliacao"


@router.post("/consultations/{cid}/nota/incluir")
def include_segment(
    cid: int,
    body: IncludeIn,
    db: Session = Depends(get_db),
    user: User = Depends(clinician),
) -> dict:
    """Painel "Fora da nota": acrescenta a fala clínica não citada à seção escolhida, com a ref Fala/N."""
    return service.include_segment(db, user, cid, body.n, body.destino)
