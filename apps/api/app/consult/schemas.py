"""Modelos da consulta assistida por IA (transcrição + Smart Apps + nota clínica + relatório cirúrgico).

Toda afirmação carrega `refs`: trechos da conversa (`Fala/N`) ou recursos FHIR (`Observation/...`). Itens marcados
com `origem="regra"` vêm da camada determinística; `origem="ia"` vêm do LLM (ou do gerador mock) e devem ser
conferidos pelo profissional.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.consult import idiomas

# falas gravadas antes da equipe cirúrgica (só médico/paciente/acompanhante) continuam válidas
Speaker = Literal["medico", "paciente", "acompanhante", "anestesista", "circulante", "instrumentador", "desconhecido"]
Severity = Literal["info", "atencao", "critico"]
Origin = Literal["regra", "ia"]
Confidence = Literal["baixa", "media", "alta"]
Priority = Literal["alta", "media", "baixa"]
ConsultKind = Literal["consulta", "visita", "cirurgia"]
CaptureSource = Literal["microfone", "servidor", "digitado", "demo", "marco"]  # marco: pausa/retomada/revogação
Marker = Literal["pausa", "retomada", "revogado"]
# qualquer código de data/reference/languages.csv (≥ 95 idiomas, BCP-47); "pt", "es-MX"… são normalizados
Language = Annotated[str, AfterValidator(idiomas.validar)]


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
    traducao_divergente: list[str] = Field(
        default_factory=list, description="diferenças de números, doses e medicamentos entre a fala e a tradução"
    )
    origem: CaptureSource = "digitado"
    relevancia: Literal["clinica", "social", "administrativa"] | None = None  # None: ainda não classificada
    relevancia_origem: Literal["regra", "ia"] = "regra"
    # edição pelo profissional (#2): o texto e o falante originais ficam guardados; o selo "editada" vem de editado_em
    texto_original: str | None = None
    falante_original: Speaker | None = None
    editado_por: str | None = None
    editado_em: str | None = None
    expurgada: bool = False  # texto apagado pelo prazo de retenção (#31)
    marco: Marker | None = None  # só nos marcos de privacidade (origem "marco"): não é fala, as regras e a IA ignoram


class SegmentEdit(BaseModel):
    """Correção de uma fala (texto e/ou quem falou) pelo profissional."""

    texto: str | None = Field(None, min_length=1, max_length=2000)
    falante: Speaker | None = None
    idioma: Language | None = None  # idioma em que a fala foi dita (chip de cada fala)


class SegmentIn(BaseModel):
    falante: Speaker = "desconhecido"
    texto: str = Field(..., min_length=1, max_length=2000)
    t: float | None = None
    idioma: Annotated[str, AfterValidator(idiomas.validar)] | None = None
    origem: CaptureSource = "digitado"


class SegmentsIn(BaseModel):
    segmentos: list[SegmentIn] = Field(..., min_length=1, max_length=50)


class FinishIn(BaseModel):
    """Corpo opcional do /finish: falas que ainda estavam na fila de captura (último resultado do reconhecimento)."""

    segmentos: list[SegmentIn] = Field(default_factory=list, max_length=50)


class ConsentPurposes(BaseModel):
    transcricao: bool
    ia_externa: bool  # análise, tradução e nota por IA externa (LLM); sem isso só as regras rodam
    traducao: bool
    emocoes: bool  # análise multidimensional (emoções)


class Consentimento(BaseModel):
    """Consentimento do paciente para a consulta, por finalidade (vira um Consent FHIR R4)."""

    quem: Literal["paciente", "acompanhante", "responsavel"]
    related_person_id: str | None = Field(None, max_length=64)
    finalidades: ConsentPurposes
    recusou: bool = False


class ConsultCreate(BaseModel):
    tipo: ConsultKind = "consulta"
    especialidade: str = "clinica_medica"
    idioma_paciente: Language | None = None  # sem idioma: vale o Patient.communication (ou pt-BR)
    consentimento: Consentimento  # obrigatório: sem registro de consentimento (ou recusa) não há consulta
    sandbox: bool = False  # cenário sintético para testar: fora do contador do plano, nunca vai ao prontuário (#34)


# ---------- Smart Apps ----------
class ItemAcionavel(BaseModel):
    """Campos comuns aos itens que o profissional aceita, descarta ou fixa (#3)."""

    item_key: str = (
        ""  # sha1(tipo + rótulo normalizado + refs ordenadas): identifica o item entre uma análise e a seguinte
    )
    primeira_fala: int = 0  # nº de falas analisadas quando o item apareceu (0 = já estava no prontuário)
    fixado: bool = False
    acao: str = ""  # última ação do profissional: aceitar | feita


class Tag(ItemAcionavel):
    rotulo: str
    status: Literal["presente", "negado"]
    refs: list[str]
    origem: Origin = "regra"
    detalhe: str = ""  # o que já se sabe da queixa ("3 sem · ao levantar"), vindo da semiologia


class Emotion(ItemAcionavel):
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


class Question(ItemAcionavel):
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


class Hypothesis(ItemAcionavel):
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
    decisao: Literal["pendente", "confirmada", "provisoria", "descartada"] = "pendente"  # decisão do profissional
    decisao_motivo: str = ""


Conciliation = Literal["prontuario", "relatado_nao_registrado", "diverge"]


class HistoryItem(BaseModel):
    texto: str
    quando: str | None = None
    tipo: Literal["diagnostico", "cirurgia", "internacao", "pronto_socorro", "consulta", "familiar", "relatado"]
    refs: list[str]
    origem: Origin = "regra"
    # antecedente ou cirurgia dito na conversa (app/consult/extract.py): estruturado e conciliado com o prontuário
    condicao: str = ""  # antecedente pessoal: nome da condição (condition_lexicon.csv)
    procedimento: str = ""  # cirurgia prévia: nome do procedimento (procedure_lexicon.csv)
    cid: str = ""
    inicio_aprox: str | None = None  # "há 5 anos (≈ 2021)", "desde a infância"
    ano: int | None = None  # ano aproximado do início ou da cirurgia
    intercorrencia: str = ""  # cirurgia: "sangramento", "reação à anestesia"
    conciliacao: Conciliation | None = None  # prontuario = no prontuário; diverge = o prontuário diz outra coisa
    observacao: str = ""


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
    origem_dado: str = "prontuário"  # "relatado (fala 7)" quando o valor veio da conversa
    comparacao: str = ""  # relatado × último resultado da série FHIR
    conciliacao: Conciliation | None = None  # relatado: coerente com a série, diverge ou sem série
    valor_num: float | None = None


class MedItem(BaseModel):
    medicamento: str
    posologia: str
    desde: str | None = None
    status: Literal["em_uso", "suspenso", "relatado", "planejado", "proposto"]
    observacao: str | None = None
    refs: list[str]


class MedSuggestion(ItemAcionavel):
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


class Highlight(ItemAcionavel):
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


class SummarySentence(BaseModel):
    """Uma frase do resumo, com a fonte (falas e/ou recursos FHIR) e a marca do que mudou desde o resumo anterior."""

    texto: str
    refs: list[str]
    nova: bool = False
    chave: str = ""  # identifica a frase entre uma análise e a seguinte (para marcar a nova)
    origem: Origin = "regra"


class Summary(BaseModel):
    texto: str  # as frases juntas (compatibilidade com quem lê só o texto)
    destaques: list[Highlight]
    frases: list[SummarySentence] = []


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


# ---------- pedidos e prescrições em rascunho (ambient orders) ----------
OrderType = Literal["medicamento", "exame"]
OrderAction = Literal["iniciar", "alterar_dose", "suspender", "solicitar"]
OrderCheckType = Literal[
    "alergia",
    "reatividade_cruzada",
    "dose_tfg",
    "dose_idade",
    "dose_peso",
    "interacao",
    "duplicidade",
    "alto_alerta",
    "retirada_abrupta",
    "sem_prescricao",
    "exame_repetido",
    "exame_pendente",
    "item_nao_reconhecido",
]


class OrderCheck(BaseModel):
    """Uma checagem de segurança feita ANTES de propor o item (alergia, dose, interação, duplicidade, repetição)."""

    tipo: OrderCheckType
    severidade: Severity
    texto: str
    refs: list[str]


class OrderDecision(BaseModel):
    """Assinatura item a item: o profissional assina ou recusa o rascunho. Item crítico exige justificativa."""

    estado: Literal["pendente", "assinado", "recusado"] = "pendente"
    justificativa: str = ""
    por: str | None = None
    em: str | None = None
    editado: bool = False  # posologia corrigida pelo profissional antes de assinar
    aviso: str = ""  # assinatura desfeita porque surgiu alerta crítico depois dela (assinar de novo, com justificativa)


class OrderDraft(BaseModel):
    """Rascunho de MedicationRequest (medicamento) ou ServiceRequest (exame) tirado da fala ou sugerido pelas regras."""

    id: str
    tipo: OrderType
    acao: OrderAction
    item: str
    chave: str  # princípio ativo (ou @classe) / chave do catálogo de exames
    dose: str = ""
    via: str = ""
    frequencia: str = ""
    duracao: str = ""
    dose_anterior: str = ""  # alterar_dose / suspender: a dose da prescrição ativa
    justificativa: str = ""
    periodicidade: dict | None = None  # {"frequencia": 3, "periodo": 1, "unidade": "d"} (FHIR Timing.repeat)
    duracao_dias: int | None = None
    refs: list[str]
    checagens: list[OrderCheck] = []
    gravidade: Severity = "info"
    fonte_pedido: Literal["fala", "protocolo", "hipotese"] = "fala"
    origem: Origin = "regra"
    loinc: str = ""
    loinc_nome: str = ""
    categoria: str = ""  # laboratorio | imagem
    preparo: str = ""
    atc: str = ""
    prescricao_ref: str = ""  # MedicationRequest ativo que o rascunho altera ou suspende
    decisao: OrderDecision = Field(default_factory=OrderDecision)


class RcriItem(BaseModel):
    """Um dos seis critérios do índice de Lee (RCRI): vale 1 ponto quando presente, sempre com a fonte."""

    criterio: str
    pontos: int
    presente: bool
    evidencia: str
    refs: list[str] = []
    sem_dado: bool = False  # não há dado no prontuário nem na conversa para decidir


class Rcri(BaseModel):
    """Risco cardíaco perioperatório (índice de Lee revisado) para a cirurgia planejada."""

    cirurgia: str
    pontos: int
    classe: str
    risco_pct: float
    interpretacao: str
    itens: list[RcriItem]
    fonte: str
    refs: list[str]


ReminderKind = Literal["alergia", "protocolo", "sinais_vitais", "pos_alta"]


class Reminder(BaseModel):
    """Lembrete ao vivo: o assunto que ainda não apareceu na conversa. Some quando o assunto é tratado."""

    chave: str
    tipo: ReminderKind
    texto: str
    refs: list[str] = []
    severidade: Severity = "info"
    fonte: str = ""


class AppItem(BaseModel):
    """Um item devolvido por um Smart App do registro (app/consult/apps): sempre com a fonte (refs)."""

    texto: str
    detalhe: str = ""
    severidade: Literal["critico", "atencao", "info", "ok"] = "info"
    refs: list[str] = []
    tags: list[str] = []  # card "tags"
    celulas: list[str] = []  # card "tabela": uma célula por coluna do manifest
    quando: str | None = None  # card "linha_do_tempo": data ou rótulo de tempo
    origem: Origin = "regra"


class AppResult(BaseModel):
    """Saída de um Smart App: o manifest (o que a tela precisa para desenhar) e os itens."""

    id: str
    nome: str
    aba: str
    card: Literal["lista", "tags", "tabela", "linha_do_tempo"]
    camada: Literal["regra", "ia", "mista"] = "regra"
    versao: str
    base_versao: str = ""
    colunas: list[str] = []
    itens: list[AppItem] = []
    vazio: str = ""  # texto do card quando não há itens
    descartados: list[dict] = []  # itens sem referência válida (nunca chegam à tela)
    erro: str = ""


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
    alertas: list[Highlight] = []  # críticos primeiro; os de atenção vêm depois (a tela os recolhe)
    lembretes: list[Reminder] = []
    relatorio_cirurgico: SurgicalReport | None = None
    pedidos: list[OrderDraft] = []
    tfg: float | None = None
    rcri: Rcri | None = None
    descartados: list[dict] = []
    regras_ms: int | None = None  # tempo das regras na última fala (chip de latência da tela)
    ocultos: int = 0  # itens que o profissional descartou ou marcou como não se aplica
    apps: list[AppResult] = []  # Smart Apps do registro; preenchido pelo serviço (o engine não o altera)


# ---------- nota final ----------
class Frase(BaseModel):
    """Uma frase da seção com a fonte: refs (Fala/N, recursos FHIR) e quem a escreveu."""

    texto: str
    refs: list[str] = []
    origem: Literal["ia", "regra", "profissional"] = "regra"


class NoteSection(BaseModel):
    texto: str
    refs: list[str]
    frases: list[Frase] = []  # proveniência do texto, frase a frase (texto continua sendo o que se lê e edita)


class SecaoModelo(BaseModel):
    """Seção do modelo de nota fora das nove clássicas (peso, vacinas, NYHA, CIAP-2...), pela `key` do modelo."""

    key: str
    texto: str
    refs: list[str]


class CidSuggestion(BaseModel):
    codigo: str
    descricao: str
    tipo: Literal["principal", "secundario", "hipotese"]
    refs: list[str]


ChronoKind = Literal["queixa", "negativa", "achado", "medicamento", "exame", "orientacao", "retorno", "emocao"]


class ChronoItem(BaseModel):
    quando: str = Field(description="mm:ss desde o início da consulta")
    texto: str
    refs: list[str]
    tipo: ChronoKind = "queixa"
    falante: Speaker | None = None


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
    secoes: list[
        SecaoModelo
    ] = []  # seções próprias do modelo de nota (note_templates); o `modelo` é guardado fora do schema


SurgicalReport.model_rebuild()
