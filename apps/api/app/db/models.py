from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app import clock
from app.db import crypto
from app.db.session import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class EncryptedJson(TypeDecorator):
    """JSON cifrado em AES-GCM (app.db.crypto): o SELECT direto na coluna só mostra texto cifrado."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else crypto.encrypt(value)

    def process_result_value(self, value, dialect):
        return None if value is None else crypto.decrypt(value)


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


class Organization(Base):
    """Organização (tenant): consultório ou hospital. Todo dado clínico do HousIA pertence a uma (#33)."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(160))
    tipo: Mapped[str] = mapped_column(String(20), default="hospital")  # consultorio | hospital
    cnes: Mapped[str | None] = mapped_column(String(7), nullable=True)  # CNES do estabelecimento (#52), 7 dígitos
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Plan(Base):
    """Catálogo comercial (#35): plano, limite de consultas e módulos liberados por padrão."""

    __tablename__ = "plans"

    codigo: Mapped[str] = mapped_column(String(20), primary_key=True)  # teste | profissional | clinica | hospital
    nome: Mapped[str] = mapped_column(String(60))
    limite_consultas: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = sem limite
    modulos_json: Mapped[list] = mapped_column(JsonType, default=list)


class Entitlement(Base):
    """Direito de uso de uma organização (#35): plano contratado, limite, módulos e vigência (ativação manual)."""

    __tablename__ = "entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), unique=True)
    plano: Mapped[str] = mapped_column(ForeignKey("plans.codigo"))
    limite_consultas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modulos_json: Mapped[list] = mapped_column(JsonType, default=list)
    inicio: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    fim: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # None = sem vencimento
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Tenant:
    """Mistura das tabelas com dono: a sessão filtra por `organization_id` e preenche ao gravar (app/db/tenancy.py)."""

    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # medico | enfermagem | gestor
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    conselho: Mapped[str | None] = mapped_column(String(10), nullable=True)  # CRM | COREN | ...
    conselho_numero: Mapped[str | None] = mapped_column(String(20), nullable=True)
    uf: Mapped[str | None] = mapped_column(String(2), nullable=True)
    email_verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    conselho_verificado: Mapped[bool] = mapped_column(Boolean, default=False)  # conferido manualmente pelo admin
    cns: Mapped[str | None] = mapped_column(String(15), nullable=True)  # CNS do profissional (#52)
    cbo: Mapped[str | None] = mapped_column(String(6), nullable=True)  # CBO-2002 do profissional (#52)


class AuthToken(Base):
    """Token de uso único por e-mail: verificação de cadastro, recuperação de senha e convite de equipe."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), index=True)  # verify | reset | invite
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 do token enviado
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)  # convite: e-mail do convidado
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # convite: papel escolhido
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class HandoffReport(Tenant, Base):
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


class Alert(Tenant, Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(20))  # handoff | medrec | discharge | jornada | consulta
    severity: Mapped[str] = mapped_column(String(20))  # info | atencao | critico
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    source_refs: Mapped[list] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="aberto")  # aberto | reconhecido | descartado
    acted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DischargeAssessment(Tenant, Base):
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


class DischargeOverride(Tenant, Base):
    __tablename__ = "discharge_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("discharge_assessments.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    justification: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MedrecRun(Tenant, Base):
    __tablename__ = "medrec_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    from_encounter_or_location: Mapped[str] = mapped_column(String(120))
    to_encounter_or_location: Mapped[str] = mapped_column(String(120))
    findings_json: Mapped[dict] = mapped_column(JsonType)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditLog(Tenant, Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AIFeedback(Tenant, Base):
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


class DischargeDocument(Tenant, Base):
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


class Consultation(Tenant, Base):
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
    # sandbox (#34): cenário sintético, fora do contador do plano; nunca vai ao prontuário
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)
    segments_json: Mapped[list] = mapped_column(EncryptedJson(), default=list)
    insights_json: Mapped[dict | None] = mapped_column(EncryptedJson(), nullable=True)
    ai_meta_json: Mapped[dict | None] = mapped_column(EncryptedJson(), nullable=True)
    insight_actions_json: Mapped[dict | None] = mapped_column(
        EncryptedJson(), nullable=True
    )  # decisões sobre cada insight (#3)
    note_json: Mapped[dict | None] = mapped_column(EncryptedJson(), nullable=True)
    note_meta_json: Mapped[dict | None] = mapped_column(EncryptedJson(), nullable=True)
    extra_json: Mapped[dict] = mapped_column(EncryptedJson(), default=dict)  # inclui cópias da nota e o copiloto
    fhir_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class EhrOutbox(Tenant, Base):
    """Fila de envio ao prontuário (#17): um registro por consulta, com o Bundle exato a enviar.

    pendente → enviando → enviado | erro. O reenvio (botão ou subida da API) manda o mesmo Bundle; como cada entrada tem
    If-None-Exist, enviar de novo nunca duplica. O Bundle tem dado clínico: cifrado e apagado depois do envio.
    """

    __tablename__ = "ehr_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pendente", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(EncryptedJson(), nullable=True)
    fhir_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class JourneyContact(Tenant, Base):
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


class JourneyTask(Tenant, Base):
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
    # pendência da consulta (#47): ligação com a consulta, criticidade e chave que impede duplicar ao finalizar de novo
    consultation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    critical: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    chave: Mapped[str | None] = mapped_column(String(160), nullable=True)


class ImpactBaseline(Tenant, Base):
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


class SmartAppProfile(Base):
    """Perfil de Smart Apps no servidor (#21): quais apps o usuário esconde, por especialidade ("" = todas).

    Substitui o localStorage: o que o profissional esconde vale em qualquer aparelho.
    """

    __tablename__ = "smart_app_profiles"
    __table_args__ = (UniqueConstraint("user_id", "especialidade", name="uq_smart_app_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    especialidade: Mapped[str] = mapped_column(String(40), default="")
    ocultos: Mapped[list] = mapped_column(JsonType, default=list)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class EstiloNota(Tenant, Base):
    """Memória de estilo da nota (#46): só preferências de forma (tamanho, siglas, ordem) por profissional.

    Guarda números por consulta (razões e contagens), nunca texto clínico de paciente.
    """

    __tablename__ = "estilo_notas"
    __table_args__ = (UniqueConstraint("user_id", name="uq_estilo_notas_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dados_json: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
