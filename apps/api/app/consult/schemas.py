"""Modelos da consulta assistida por IA (transcrição + Smart Apps + nota clínica + relatório cirúrgico).

Toda afirmação carrega `refs`: trechos da conversa (`Fala/N`) ou recursos FHIR (`Observation/...`). Itens marcados
com `origem="regra"` vêm da camada determinística; `origem="ia"` vêm do LLM (ou do gerador mock) e devem ser
conferidos pelo profissional.
"""

from typing import Literal

from pydantic import BaseModel, Field

Speaker = Literal["medico", "paciente", "acompanhante", "desconhecido"]
Severity = Literal["info", "atencao", "critico"]
Origin = Literal["regra", "ia"]
Confidence = Literal["baixa", "media", "alta"]
Priority = Literal["alta", "media", "baixa"]
ConsultKind = Literal["consulta", "visita", "cirurgia"]
CaptureSource = Literal["microfone", "servidor", "digitado", "demo"]
Language = Literal["pt-BR", "es", "en", "fr", "it", "de", "zh", "ar", "ht"]

LANGUAGE_LABELS: dict[str, str] = {
    "pt-BR": "Português",
    "es": "Español",
    "en": "English",
    "fr": "Français",
    "it": "Italiano",
    "de": "Deutsch",
    "zh": "中文",
    "ar": "العربية",
    "ht": "Kreyòl ayisyen",
}


def fala_ref(n: int) -> str:
    return f"Fala/{n}"


# ---------- transcrição ----------
class Segment(BaseModel):
    n: int
    t: float = Field(description="segundos desde o início da consulta")
    falante: Speaker
    falante_auto: bool = False
    texto: str
    idioma: str = "pt-BR"
    traducao: str | None = None
    origem: CaptureSource = "digitado"


class SegmentIn(BaseModel):
    falante: Speaker = "desconhecido"
    texto: str = Field(..., min_length=1, max_length=2000)
    t: float | None = None
    idioma: str | None = None
    origem: CaptureSource = "digitado"


class SegmentsIn(BaseModel):
    segmentos: list[SegmentIn] = Field(..., min_length=1, max_length=50)


class ConsultCreate(BaseModel):
    tipo: ConsultKind = "consulta"
    especialidade: str = "clinica_medica"
    idioma_paciente: Language = "pt-BR"


# ---------- Smart Apps ----------
class Tag(BaseModel):
    rotulo: str
    status: Literal["presente", "negado"]
    refs: list[str]
    origem: Origin = "regra"


class Emotion(BaseModel):
    emocao: str
    rotulo: str
    quem: Speaker
    intensidade: Literal["baixa", "media", "alta"]
    trecho: str
    abordagem: str
    refs: list[str]
    origem: Origin = "regra"


class EmotionPoint(BaseModel):
    n: int
    falante: Speaker
    emocoes: list[str]


class CommMetrics(BaseModel):
    falas: int
    palavras_paciente_pct: int
    palavras_profissional_pct: int
    perguntas_abertas: int
    perguntas_fechadas: int


class Question(BaseModel):
    pergunta: str
    motivo: str
    prioridade: Priority
    refs: list[str]
    origem: Origin = "regra"


class Evidence(BaseModel):
    texto: str
    refs: list[str]


class Hypothesis(BaseModel):
    hipotese: str
    cid: str
    confianca: Confidence
    a_favor: list[Evidence]
    contra: list[Evidence]
    proximos_passos: str
    sinais_de_alarme: str
    refs: list[str]
    origem: Origin = "regra"


class HistoryItem(BaseModel):
    texto: str
    quando: str | None = None
    tipo: Literal["diagnostico", "cirurgia", "internacao", "pronto_socorro", "consulta", "familiar", "relatado"]
    refs: list[str]
    origem: Origin = "regra"


class AllergyInfo(BaseModel):
    alergeno: str
    reacao: str
    fonte: Literal["prontuario", "relatado"]
    reatividade_cruzada: str
    evitar: str
    alternativas: str
    dicas: str
    conflitos: list[str]
    refs: list[str]
    origem: Origin = "regra"


class ExamInfo(BaseModel):
    exame: str
    chave: str
    valor: str
    data: str
    status: Literal["baixo", "normal", "alto"]
    explicacao: str
    tendencia: str | None = None
    refs: list[str]
    origem: Origin = "regra"


class MedItem(BaseModel):
    medicamento: str
    posologia: str
    desde: str | None = None
    status: Literal["em_uso", "suspenso", "relatado"]
    observacao: str | None = None
    refs: list[str]


class MedSuggestion(BaseModel):
    texto: str
    tipo: Literal["ajuste_dose", "alternativa", "suspender", "interacao", "registrar", "adesao", "alergia"]
    severidade: Severity
    refs: list[str]
    origem: Origin = "regra"


class SurgeryItem(BaseModel):
    texto: str
    tipo: Literal["historico", "planejada", "checagem"]
    refs: list[str]
    origem: Origin = "regra"


class ProtocolItem(BaseModel):
    protocolo: str
    item: str
    status: Literal["em_dia", "pendente", "sem_registro", "fora_do_alvo", "no_alvo"]
    ultimo: str | None = None
    orientacao: str
    refs: list[str]


class Highlight(BaseModel):
    texto: str
    severidade: Severity
    refs: list[str]


class Anamnese(BaseModel):
    queixa_principal: str
    hda: str
    antecedentes: list[str]
    medicacoes_em_uso: list[str]
    alergias: list[str]
    habitos_e_contexto: list[str]
    revisao_de_sistemas: list[str]
    refs: list[str]


class Summary(BaseModel):
    texto: str
    destaques: list[Highlight]


# ---------- relatório cirúrgico (ditado) ----------
class ReportField(BaseModel):
    campo: str
    rotulo: str
    texto: str
    refs: list[str]
    obrigatorio: bool = False


class OpmeLine(BaseModel):
    codigo: str
    descricao: str
    autorizado: int
    citado: int
    status: Literal["conferido", "nao_autorizado", "nao_citado", "quantidade_divergente"]
    refs: list[str]


class SurgicalReport(BaseModel):
    campos: list[ReportField]
    opme: list[OpmeLine]
    pendencias: list[Highlight]
    cids: list["CidSuggestion"]


class Insights(BaseModel):
    tipo: ConsultKind
    especialidade: str
    falas_analisadas: int
    modo: Literal["regras", "ia"]
    queixas: list[Tag] = []
    emocoes: list[Emotion] = []
    linha_emocional: list[EmotionPoint] = []
    comunicacao: CommMetrics | None = None
    perguntas: list[Question] = []
    perguntas_abordadas: list[Question] = []
    hipoteses: list[Hypothesis] = []
    historico: list[HistoryItem] = []
    alergias: list[AllergyInfo] = []
    exames: list[ExamInfo] = []
    medicamentos: list[MedItem] = []
    sugestoes_medicamentos: list[MedSuggestion] = []
    cirurgia: list[SurgeryItem] = []
    protocolos: list[ProtocolItem] = []
    resumo: Summary | None = None
    anamnese: Anamnese | None = None
    alertas: list[Highlight] = []
    relatorio_cirurgico: SurgicalReport | None = None
    tfg: float | None = None
    descartados: list[dict] = []


# ---------- nota final ----------
class NoteSection(BaseModel):
    texto: str
    refs: list[str]


class CidSuggestion(BaseModel):
    codigo: str
    descricao: str
    tipo: Literal["principal", "secundario", "hipotese"]
    refs: list[str]


class ChronoItem(BaseModel):
    quando: str = Field(description="mm:ss desde o início da consulta")
    texto: str
    refs: list[str]


class ConsultNote(BaseModel):
    """Nota clínica estruturada (anamnese + SOAP), revisada e aprovada pelo profissional antes de ir ao prontuário."""

    queixa_principal: NoteSection
    hda: NoteSection
    antecedentes: NoteSection
    medicacoes: NoteSection
    alergias: NoteSection
    exame_fisico: NoteSection
    exames_complementares: NoteSection
    avaliacao: NoteSection
    plano: NoteSection
    cids: list[CidSuggestion]
    cronologia: list[ChronoItem]
    pendencias: list[NoteSection]
    orientacoes_paciente: list[str]


SurgicalReport.model_rebuild()
