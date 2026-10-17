from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app import clock
from app.db.session import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """DateTime sempre com fuso: o SQLite devolve datas sem tzinfo; tratamos como UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def utcnow() -> datetime:
    return clock.now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # medico | enfermagem | gestor
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class HandoffReport(Base):
    __tablename__ = "handoff_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audience: Mapped[str] = mapped_column(String(20))  # medico | enfermagem
    from_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    window_hours: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[dict] = mapped_column(JsonType)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(20))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(20))  # handoff | medrec | discharge | jornada
    severity: Mapped[str] = mapped_column(String(20))  # info | atencao | critico
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    source_refs: Mapped[list] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="aberto")  # aberto | reconhecido | descartado
    acted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DischargeAssessment(Base):
    __tablename__ = "discharge_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    social_checklist_json: Mapped[dict] = mapped_column(JsonType)
    lace_components_json: Mapped[dict] = mapped_column(JsonType)
    lace_score: Mapped[int] = mapped_column(Integer)
    social_score: Mapped[int] = mapped_column(Integer)
    final_risk: Mapped[str] = mapped_column(String(20))  # baixo | moderado | alto
    suggestions_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DischargeOverride(Base):
    __tablename__ = "discharge_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("discharge_assessments.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    justification: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MedrecRun(Base):
    __tablename__ = "medrec_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    from_encounter_or_location: Mapped[str] = mapped_column(String(120))
    to_encounter_or_location: Mapped[str] = mapped_column(String(120))
    findings_json: Mapped[dict] = mapped_column(JsonType)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AIFeedback(Base):
    """Avaliação "útil / não útil" de uma saída da IA (medida de qualidade para o piloto)."""

    __tablename__ = "ai_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    patient_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))  # handoff | handoff_check | medrec | discharge | discharge_docs
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    useful: Mapped[bool] = mapped_column()
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # kind "nps": nota de 0 a 10
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DischargeDocument(Base):
    """Sumário de alta (SOAP) e orientações ao paciente/cuidador gerados para uma avaliação de alta."""

    __tablename__ = "discharge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("discharge_assessments.id"), nullable=True)
    language: Mapped[str] = mapped_column(String(10))
    content_json: Mapped[dict] = mapped_column(JsonType)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Consultation(Base):
    """Consulta assistida por IA: transcrição, Smart Apps, nota revisada e envio ao prontuário.

    Só texto: o áudio nunca é gravado. A transcrição fica no banco do HousIA (não vai para o audit_log).
    """

    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # consulta | visita | cirurgia
    specialty: Mapped[str] = mapped_column(String(40))
    patient_language: Mapped[str] = mapped_column(String(10), default="pt-BR")
    status: Mapped[str] = mapped_column(String(20), default="em_andamento")  # em_andamento | finalizada | enviada
    segments_json: Mapped[list] = mapped_column(JsonType, default=list)
    insights_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    ai_meta_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    note_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    note_meta_json: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    extra_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    fhir_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class JourneyContact(Base):
    """Contato pós-alta feito pela equipe (ligação, visita ou retorno de mensagem): roteiro respondido e desfecho.

    O HousIA não envia SMS/WhatsApp: registra o que a equipe fez. O texto livre e as respostas ficam aqui; o
    prontuário recebe um DocumentReference (fhir_ref).
    """

    __tablename__ = "journey_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(String(20))  # telefone | visita | presencial | mensagem
    reached: Mapped[bool] = mapped_column()  # falou com o paciente/cuidador (tentativa sem sucesso = False)
    answers_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    alarm_signs_json: Mapped[list] = mapped_column(JsonType, default=list)
    pendencies_json: Mapped[list] = mapped_column(JsonType, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    fhir_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class JourneyTask(Base):
    """Tarefa de acompanhamento criada a partir de um item da lista da jornada (espelhada como FHIR Task)."""

    __tablename__ = "journey_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    rule: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(20))  # papel responsável: medico | enfermagem | gestor
    due_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="aberta")  # aberta | concluida | cancelada
    source_refs: Mapped[list] = mapped_column(JsonType, default=list)
    fhir_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImpactBaseline(Base):
    """Linha de base por setor ("tempo médio de documentação antes do HousIA"), informada pela gestão.

    Histórico só de acréscimo: vale a linha mais recente de cada setor; as anteriores ficam para auditoria.
    """

    __tablename__ = "impact_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sector: Mapped[str] = mapped_column(String(60), index=True)  # especialidade da consulta
    minutes_before: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
