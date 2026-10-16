"""Modelos da consulta assistida por IA (transcrição + Smart Apps + nota clínica + relatório cirúrgico).

Toda afirmação carrega `refs`: trechos da conversa (`Fala/N`) ou recursos FHIR (`Observation/...`). Itens marcados
com `origem="regra"` vêm da camada determinística; `origem="ia"` vêm do LLM (ou do gerador mock) e devem ser
conferidos pelo profissional.
"""

from typing import Literal

from pydantic import BaseModel, Field

# falas gravadas antes da equipe cirúrgica (só médico/paciente/acompanhante) continuam válidas
Speaker = Literal["medico", "paciente", "acompanhante", "anestesista", "circulante", "instrumentador", "desconhecido"]
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
    detalhe: str = ""  # o que já se sabe da queixa ("3 sem · ao levantar"), vindo da semiologia


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
    refs: list[str]  # sugerida: o que motiva a pergunta; abordada: a fala do profissional que a fez
    origem: Origin = "regra"
    id: str = ""  # id da linha em consult_questions.csv (a IA devolve ids ao dizer o que já foi perguntado)
    fonte: str = ""  # diretriz de origem da pergunta
    resposta: str = ""  # abordada: o que o paciente respondeu
    resposta_refs: list[str] = []  # abordada: a fala do paciente que respondeu
    discrimina: list[str] = []  # hipóteses de maior confiança que a resposta ajuda a separar


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
    cid_descricao: str = ""  # descrição oficial do CID (cid10.csv); vazio quando o CID não existe
    pergunta_discriminante: str = ""  # a pergunta que mais separa esta hipótese das vizinhas
    exame_discriminante: str = ""  # o exame que a confirma ou afasta
    fonte: str = ""  # diretriz de onde vem a linha curada


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
    chave: str = ""  # alérgeno na base de reatividade cruzada: princípio ativo, "@classe" ou "alimento:..."
    criticidade: Literal["alta", "baixa", "indeterminada"] = "indeterminada"  # FHIR AllergyIntolerance.criticality
    fonte_regra: str = ""  # de onde vem a reatividade cruzada (allergy_cross.csv)


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
    status: Literal["em_uso", "suspenso", "relatado", "planejado"]
    observacao: str | None = None
    refs: list[str]


class MedSuggestion(BaseModel):
    texto: str
    tipo: Literal[
        "ajuste_dose",
        "alternativa",
        "suspender",
        "interacao",
        "registrar",
        "adesao",
        "alergia",
        "efeito_adverso",
        "alto_alerta",
    ]
    severidade: Severity
    refs: list[str]
    origem: Origin = "regra"
    fonte: str = ""  # regra de onde vem o alerta (dose_alerts.csv, interactions.csv, allergy_cross.csv...)


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
    categoria: str | None = None  # ex.: "opme" (recalculada quando o cirurgião edita o OPME na revisão)


SemioField = Literal["inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados"]


class CampoSemio(BaseModel):
    """Um campo semiológico: o valor dito e as falas de onde veio. Vazio = ainda não foi dito."""

    valor: str = ""
    refs: list[str] = []


class QueixaDetalhada(BaseModel):
    """Semiologia de uma queixa presente: cada campo é {valor, refs}; `faltam` lista o que ainda não foi perguntado."""

    chave: str
    rotulo: str
    refs: list[str]
    inicio_duracao: CampoSemio = Field(default_factory=CampoSemio)
    localizacao: CampoSemio = Field(default_factory=CampoSemio)
    intensidade: CampoSemio = Field(default_factory=CampoSemio)
    piora: CampoSemio = Field(default_factory=CampoSemio)
    melhora: CampoSemio = Field(default_factory=CampoSemio)
    frequencia: CampoSemio = Field(default_factory=CampoSemio)
    associados: CampoSemio = Field(default_factory=CampoSemio)
    nega: CampoSemio = Field(default_factory=CampoSemio)  # sintomas negados na mesma conversa ("sem desmaio")
    faltam: list[SemioField] = []


class CampoEspecialidade(BaseModel):
    """Campo extra da anamnese de uma especialidade (pediatria: gestação e parto, vacinas, DNPM)."""

    campo: str
    rotulo: str
    valor: str = ""
    refs: list[str] = []


class Anamnese(BaseModel):
    queixa_principal: str
    hda: str
    antecedentes: list[str]
    medicacoes_em_uso: list[str]
    alergias: list[str]
    habitos_e_contexto: list[str]
    revisao_de_sistemas: list[str]
    refs: list[str]
    qp_principal: str = ""  # chave da queixa principal (a primeira dita, ou a que o profissional fixou)
    qp_fixada: bool = False
    queixas_detalhadas: list[QueixaDetalhada] = []
    campos_especialidade: list[CampoEspecialidade] = []


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
    motivo: str | None = None
    fonte: str | None = None


OpmeStatus = Literal[
    "conferido",
    "nao_autorizado",
    "nao_citado",
    "quantidade_divergente",
    "validade_vencida",
    "sem_etiqueta",
    "sem_fala",
]
ViaStatus = Literal["ok", "divergente", "ausente", "nao_registrado", "nao_aplica"]


class OpmeLote(BaseModel):
    """Leitura de etiqueta GS1 (lote, validade, série) de um item consumido."""

    id: str
    gtin: str | None = None
    lote: str | None = None
    validade: str | None = None
    serie: str | None = None
    quantidade: int = 1
    vencido: bool = False
    registro_anvisa: str | None = None
    lido_em: str | None = None
    por: str | None = None


class OpmeLine(BaseModel):
    """Conciliação em três vias: o narrado (Fala/N), o autorizado (guia) e o consumido (etiqueta lida)."""

    codigo: str
    descricao: str
    autorizado: int
    citado: int
    consumido: int = 0
    status: OpmeStatus
    refs: list[str]
    status_fala: ViaStatus = "ok"
    status_guia: ViaStatus = "ok"
    status_etiqueta: ViaStatus = "nao_registrado"
    implantavel: bool = False
    tamanhos: list[str] = []
    lotes: list[OpmeLote] = []
    registro_anvisa: str | None = None
    justificativa: str | None = None
    editado: bool = False


class TemplateInfo(BaseModel):
    """Modelo do relatório em uso (data/reference/surgical_templates) e como foi escolhido."""

    id: str
    rotulo: str
    especialidade: str
    especialidade_rotulo: str
    versao: str
    versao_base: str
    origem: Literal["ditado", "agendamento", "especialidade", "base"]
    refs: list[str]


class ScheduledInfo(BaseModel):
    procedimento: str
    sitio: str | None = None
    lado: str | None = None
    refs: list[str]


class SafetyCheck(BaseModel):
    """Verificação perioperatória (Cirurgia Segura, sítio, contagem, profilaxia), sempre com a fonte."""

    chave: str
    grupo: Literal["cirurgia_segura", "sitio", "contagem", "intraoperatorio", "profilaxia"]
    rotulo: str
    status: Literal["ok", "pendente", "info", "atencao", "critico"]
    texto: str
    fonte: str
    refs: list[str]
    marcado_por: str | None = None
    em: str | None = None


class ProposedOrder(BaseModel):
    """Exame citado no ditado que vira pedido proposto (FHIR ServiceRequest, intent=proposal) para o RIS."""

    tipo: Literal["imagem"] = "imagem"
    descricao: str
    motivo: str
    refs: list[str]
    recurso: dict


class BillingItem(BaseModel):
    """Pendência de faturamento: a regra (em data/reference/glosa_rules) que a gerou, com a fonte."""

    regra: str
    tipo: str
    severidade: Severity
    texto: str
    fonte: str
    campo: str | None = None
    refs: list[str] = []
    peso: int = 0


class Readiness(BaseModel):
    """Prontidão para faturamento (0–100): regras de glosa em dados, avaliadas sobre o relatório."""

    score: int
    pronto: bool
    convenio: str
    convenio_rotulo: str
    regras_avaliadas: int
    regras_ok: int
    criticas: int
    atencoes: int
    pendencias: list[BillingItem]
    procedimento: str | None = None
    codigo_demo: str | None = None
    numero_guia: str | None = None
    versao: str = ""


class SurgicalReport(BaseModel):
    campos: list[ReportField]
    opme: list[OpmeLine]
    pendencias: list[Highlight]
    cids: list["CidSuggestion"]
    modelo: TemplateInfo | None = None
    agendado: ScheduledInfo | None = None
    verificacoes: list[SafetyCheck] = []
    pedidos: list[ProposedOrder] = []
    perioperatorio: list["SurgeryItem"] = []
    prontidao: Readiness | None = None


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
