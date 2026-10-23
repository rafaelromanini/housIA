"use client";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const CSRF_COOKIE = "housia_csrf";

export type Role = "medico" | "enfermagem" | "gestor";
export type Severity = "info" | "atencao" | "critico";
export type Risk = "baixo" | "moderado" | "alto";

export interface Organizacao {
  id: number;
  nome: string;
  tipo: "consultorio" | "hospital";
}

export type ModuloHospitalar = "passagem_plantao" | "reconciliacao" | "alta";

export interface PlanoResumo {
  codigo: "teste" | "profissional" | "clinica" | "hospital";
  nome: string;
  modulos: ModuloHospitalar[];
  limite: number | null;
  usadas: number;
  aviso: string | null;
}

export interface User {
  id: number;
  name: string;
  email: string;
  role: Role;
  organizacao: Organizacao | null;
  plano: PlanoResumo | null;
  destino: string;
  conselho: string | null;
  conselho_numero: string | null;
  uf: string | null;
  conselho_verificado: boolean;
  email_verificado: boolean;
}

export interface AlertCounts {
  critico: number;
  atencao: number;
  info: number;
}

export interface BedRow {
  id: string;
  nome: string;
  idade: number | null;
  sexo: string;
  setor: string;
  leito: string;
  internado_em: string | null;
  dias_internacao: number;
  horas_no_setor: number | null;
  motivo: string;
  alertas_abertos: AlertCounts;
  risco_alta: Risk | null;
  cenario: string | null;
}

export interface ContextItem {
  source_ref: string;
  summary: string;
  detail: Record<string, unknown> | null;
}

export interface PatientDetail {
  id: string;
  nome: string;
  idade: number | null;
  sexo: string;
  setor: string | null;
  leito: string | null;
  internado_em: string | null;
  dias_internacao: number;
  tipo_internacao: string;
  motivo: string;
  encounter_id: string | null;
  setores: { encounter_id: string; setor: string; leito: string; inicio: string | null; fim: string | null }[];
  atendimentos_urgencia_anteriores: { source_ref: string; inicio: string; motivo: string }[];
  contexto: {
    diagnosticos: ContextItem[];
    alergias: ContextItem[];
    medicamentos_ativos: ContextItem[];
    familiares: ContextItem[];
    nega_alergias: boolean;
  };
  alertas_abertos: AlertCounts;
  risco_alta: Risk | null;
  cenario: { id: string; tag: string; titulo: string; esperado: string } | null;
  internado: boolean;
  ambulatorial: {
    encounter_id: string;
    servico: string;
    status: string;
    inicio: string | null;
    motivo: string;
    profissional: string;
  } | null;
}

export type EventKind = "vital" | "lab" | "med_order" | "med_admin" | "note" | "procedure" | "transfer" | "diagnosis" | "io" | "consent";

export interface ClinicalEvent {
  id: string;
  patient_id: string;
  timestamp: string;
  kind: EventKind;
  summary: string;
  value: Record<string, unknown> | null;
  source_ref: string;
  encounter_id: string | null;
  location: string | null;
  severity: "normal" | "atencao" | "critico" | null;
  text: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown,
  ) {
    super(message);
  }
}

/** Token CSRF (double submit): o cookie de sessão é httpOnly e o JS nunca o lê; só este cookie é legível. */
export function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const hit = document.cookie.split("; ").find((c) => c.startsWith(`${CSRF_COOKIE}=`));
  return hit ? decodeURIComponent(hit.slice(CSRF_COOKIE.length + 1)) : null;
}

/** Encerra a sessão: o servidor apaga os cookies. */
export async function endSession(): Promise<void> {
  try {
    await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include" });
  } catch {
    /* sem rede: o cookie expira sozinho */
  }
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const csrf = csrfToken();
  if (csrf && !["GET", "HEAD", "OPTIONS"].includes((init.method ?? "GET").toUpperCase())) headers.set("X-CSRF-Token", csrf);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`${API_URL}${path}`, { ...init, headers, body, credentials: "include" });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
    window.location.href = "/login";
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : res.statusText;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail), data);
  }
  return data as T;
}

// ---------- Passagem de plantão ----------
export type Audience = "medico" | "enfermagem";

export interface ItemRef {
  texto: string;
  refs: string[];
}

export interface HandoffRisk extends ItemRef {
  horizonte_h: number;
  severidade: Severity;
  alert_id?: number;
}

export interface HandoffPending extends ItemRef {
  horario: string;
}

export interface Fact {
  tipo: string;
  texto: string;
  severidade: Severity;
  refs: string[];
  publico: Audience[];
  horario: string | null;
  horizonte_h: number | null;
}

export interface HandoffContent {
  headline: string;
  situacao: string;
  situacao_refs: string[];
  mudancas_no_periodo: ItemRef[];
  riscos_proximas_horas: HandoffRisk[];
  pendencias: HandoffPending[];
  perguntas_para_quem_sai: string[];
  fatos: Fact[];
  descartados: { secao: string; texto: string; refs_invalidas: string[]; motivo: string }[];
  meta: {
    gerado_em: string;
    janela_h: number;
    publico: Audience;
    destino: string | null;
    modo: "mock" | "real";
    reparado: boolean;
    eventos_na_janela: number;
    pseudonimizacoes: number;
  };
}

export interface HandoffReport {
  id: number;
  patient_id: string;
  audience: Audience;
  window_hours: number;
  to_location: string | null;
  created_at: string;
  model: string;
  prompt_version: string;
  cached: boolean;
  content: HandoffContent;
}

export interface AlertItem {
  id: number;
  patient_id: string;
  source: "handoff" | "medrec" | "discharge" | "jornada" | "consulta";
  severity: Severity;
  title: string;
  detail: string;
  source_refs: string[];
  status: "aberto" | "reconhecido" | "descartado";
  acted_by: number | null;
  acted_at: string | null;
  created_at: string;
}

// ---------- Reconciliação medicamentosa ----------
export interface SectorRef {
  encounter_id: string;
  setor: string;
  leito: string;
  inicio: string | null;
  fim: string | null;
}

export interface MedSummary {
  ref: string;
  texto: string;
  dose: string;
  via: string;
  horarios: string[];
  se_necessario: boolean;
  status: string;
  motivo: string | null;
}

export interface RecRow {
  principio_ativo: string;
  nome: string;
  classe: string;
  classe_rotulo: string;
  origem: MedSummary | null;
  destino: MedSummary | null;
  status: "mantida" | "alterada" | "suspensa" | "omitida" | "nova";
  observacao: string | null;
  refs: string[];
}

export interface MedFinding {
  tipo: "omissao" | "duplicidade" | "interacao" | "alto_alerta" | "alergia" | "alergia_cruzada";
  severidade: Severity;
  titulo: string;
  detalhe: string;
  recomendacao: string;
  refs: string[];
  medicamentos: string[];
  alert_id?: number;
}

export interface Hypothesis {
  texto: string;
  medicamento: string;
  mecanismo: "retirada" | "efeito_adverso" | "interacao";
  confidence: "baixa" | "media" | "alta";
  refs: string[];
}

export interface ProposedRx {
  ref: string;
  nome: string;
  classe: string;
  classe_rotulo: string;
  texto: string;
  acao: "nova" | "substitui";
  substitui: string | null;
  prescritor: string | null;
  em: string | null;
  justificativa: string | null;
  alertas: string[];
}
export interface MedrecRun {
  id: number;
  patient_id: string;
  created_at: string;
  origem: SectorRef | null;
  destino: SectorRef;
  linhas: RecRow[];
  /** Prescrições propostas e assinadas na consulta com IA (MedicationRequest draft/proposal), conferidas aqui. */
  prescricoes_propostas?: ProposedRx[];
  achados: MedFinding[];
  hipoteses: Hypothesis[];
  sinais_detectados: { sinal: string; texto: string; ref: string; timestamp: string }[];
  meta: {
    modo: "mock" | "real" | null;
    modelo: string | null;
    prompt_version: string | null;
    gerado_em: string;
    descartados: { texto: string; refs_invalidas: string[] }[];
    aviso: string;
  };
}

// ---------- Planejamento de alta ----------
export interface SocialChecklist {
  mora_sozinho: boolean | null;
  tem_cuidador: boolean | null;
  cuidador_capacitado: boolean | null;
  escadas_em_casa: boolean | null;
  mobilidade_reduzida: boolean | null;
  acesso_medicamentos: boolean | null;
  precisa_dispositivos: boolean | null;
  dispositivos: string[];
  ja_tem_dispositivos: boolean | null;
  consulta_retorno_agendada: boolean | null;
  teach_back_realizado: boolean | null;
  observacoes?: string | null;
}

export interface LaceComponent {
  rotulo: string;
  valor: string;
  pontos: number;
  refs: string[];
  categorias?: { categoria: string; peso: number; cid10: string; refs: string[] }[];
}

export interface DischargeSuggestion {
  texto: string;
  motivo: string;
  prioridade: "alta" | "media" | "baixa";
  responsavel: string;
  refs: string[];
}

export interface DischargeAssessment {
  id: number;
  patient_id: string;
  created_at: string;
  checklist: SocialChecklist;
  lace: { score: number; banda: Risk; componentes: Record<"L" | "A" | "C" | "E", LaceComponent> };
  social: {
    score: number;
    banda: Risk;
    itens: { campo: string; rotulo: string; peso: number; refs: string[] }[];
    aviso: string;
    faixas: { moderado: number; alto: number };
  };
  final_risk: Risk;
  explicacao: string;
  sugestoes: DischargeSuggestion[];
  descartados: DischargeSuggestion[];
  meta: { modo: "mock" | "real"; modelo: string; prompt_version: string };
  trava: { ativa: boolean; motivo: string | null };
  overrides: { id: number; usuario: string; papel: string; justificativa: string; created_at: string }[];
  confirmada: { em: string; por: string | null } | null;
}

export interface DischargePrefill {
  valores: Partial<Pick<SocialChecklist, "mora_sozinho" | "tem_cuidador" | "mobilidade_reduzida">>;
  fontes: Record<string, { ref: string | null; explicacao: string }>;
}

// ---------- Auditoria ----------
export interface AuditEntry {
  id: number;
  created_at: string;
  user: { id: number; name: string; role: Role } | null;
  action: string;
  entity: string;
  entity_id: string | null;
  payload: Record<string, unknown>;
}

export interface AuditMetrics {
  periodo: { inicio: string; fim: string };
  feedback_ia: Record<string, { util: number; nao_util: number }>;
  taxa_utilidade_ia: number | null;
  taxa_aceitacao_alertas: number | null;
  alertas_criticos: number;
  alertas_criticos_por_origem: Record<string, number>;
  alertas_por_status: Record<string, number>;
  overrides: number;
  altas_alto_risco: number;
  altas_confirmadas: number;
  passagens_geradas: number;
  passagens_do_cache: number;
  reconciliacoes: number;
  itens_ia_descartados_sem_fonte: number;
  consulta_ia: ImpactReport;
}

// ---------- Painel Impacto (#36) ----------
export interface ImpactMetric {
  grupo: string;
  rotulo: string;
  valor: number | null;
  unidade: string;
  formula: string;
  n_linhas: number;
  numerador?: number;
  denominador?: number;
  por_camada?: Record<string, { sugeridas: number; aceitas: number; pct: number | null }>;
  tabela?: { profissional: string; papel: string | null; mes: string; consultas: number }[];
  distribuicao?: { promotores: number; neutros: number; detratores: number };
  respostas?: number;
  antes?: number | null;
  depois?: number | null;
  glosas_evitadas?: number;
  minutos?: number;
  por_setor?: {
    setor: string;
    rotulo: string;
    atendimentos: number;
    tempo_atual_min: number | null;
    linha_de_base_min: number | null;
    minutos_devolvidos: number | null;
  }[];
  sem_linha_de_base?: string[];
}

export interface ImpactReport {
  periodo: { inicio: string; fim: string };
  grupos: { id: string; rotulo: string; metricas: string[] }[];
  metricas: Record<string, ImpactMetric>;
}

export interface DrillItems<T> {
  rotulo: string;
  valor: number | null;
  unidade: string;
  formula: string;
  total: number;
  items: T[];
}

export interface ImpactBaseline {
  id: number;
  setor: string;
  rotulo: string;
  minutos_antes: number;
  observacao: string | null;
  por: string | null;
  em: string;
}

// ---------- Painel de gestão (#37) ----------
export interface GestaoLine {
  id: string;
  descricao: string;
  paciente: string | null;
  setor: string;
  fhir_refs: string[];
  peso: number;
}

export interface GestaoIndicator {
  grupo: string;
  rotulo: string;
  valor: number | null;
  unidade: string;
  formula: string;
  n_linhas: number;
  por_setor: Record<string, number>;
  numerador?: number;
  denominador?: number;
  por_transicao?: Record<string, number>;
  ranking?: { cid: string; descricao: string; n: number }[];
}

export interface GestaoReport {
  periodo: { inicio: string; fim: string };
  gerado_em: string;
  grupos: { id: string; rotulo: string; indicadores: string[] }[];
  indicadores: Record<string, GestaoIndicator>;
  serie_semanal: Record<string, { semana: string; inicio: string; fim: string; valor: number | null }[]>;
  comparacao_setores: { setor: string; valores: Record<string, number> }[];
}

export interface GestaoSummary {
  frases: { texto: string; refs: string[] }[];
  descartados: { texto: string; motivo: string }[];
  meta: { modo: string; modelo: string; prompt_version: string };
  aviso: string;
}

/** Baixa um arquivo da API (com o cookie de sessão) e o entrega ao navegador. */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, { credentials: "include" });
  if (!res.ok) {
    // devolve o motivo do servidor (ex.: 409 de revisão pendente) em vez de uma mensagem genérica
    const data = await res.json().catch(() => null);
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
    throw new ApiError(res.status, typeof detail === "string" ? detail : "Não foi possível exportar.", data);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------- Melhorias (benchmark): feedback, conferência verbal, documentos de alta ----------
export type FeedbackKind = "handoff" | "handoff_check" | "medrec" | "discharge" | "discharge_docs" | "consulta" | "consulta_nota" | "nps";

export interface HandoffCheckItem {
  fato: string;
  status: "mencionado" | "omitido" | "divergente";
  severidade: Severity;
  comentario: string;
  refs: string[];
}

export interface HandoffCheck {
  resumo: string;
  itens: HandoffCheckItem[];
  contagem: { mencionado: number; omitido: number; divergente: number };
  cobertura: number | null;
  descartados: number;
  meta: { modo: "mock" | "real"; modelo: string; prompt_version: string; gerado_em: string; publico: Audience };
}

export interface DocSection {
  texto: string;
  refs: string[];
  sem_fonte?: boolean;
}

export interface DischargeDocuments {
  id: number;
  patient_id: string;
  idioma: string;
  created_at: string;
  model: string;
  prompt_version: string;
  soap: { subjetivo: DocSection; objetivo: DocSection; avaliacao: DocSection; plano: DocSection };
  orientacoes: {
    titulo: string;
    resumo_internacao: DocSection;
    medicamentos: { medicamento: string; como_usar: string; horarios: string[]; para_que: string; refs: string[] }[];
    sinais_de_alerta: DocSection[];
    cuidados_em_casa: DocSection[];
    retorno: DocSection;
  };
  descartados: { onde: string; texto?: string; refs_invalidas: string[] }[];
  /** Cartão do implante: vem do Device FHIR registrado na cirurgia, não do LLM. */
  cartao_implante?: ImplantCard[];
  meta: { modo: "mock" | "real"; gerado_em: string; traducao_disponivel: boolean };
}

export interface ImplantCard {
  descricao: string;
  lote: string | null;
  serie: string | null;
  validade: string | null;
  registro_anvisa: string | null;
  gtin: string | null;
  procedimento: string | null;
  colocado_em: string | null;
  refs: string[];
}

// ---------- Consulta com IA ----------
export type Speaker = "medico" | "paciente" | "acompanhante" | "anestesista" | "circulante" | "instrumentador" | "desconhecido";
export type ConsultKind = "consulta" | "visita" | "cirurgia";
export type Origin = "regra" | "ia";
export type Priority = "alta" | "media" | "baixa";
export type Confidence = "baixa" | "media" | "alta";

export interface Segment {
  n: number;
  t: number;
  falante: Speaker;
  falante_auto: boolean;
  texto: string;
  idioma: string;
  traducao: string | null;
  /** diferenças de números, doses e medicamentos entre a fala e a tradução (checagem determinística) */
  traducao_divergente?: string[];
  origem: "microfone" | "servidor" | "digitado" | "demo" | "marco";
  /** edição pelo profissional (#2): o original fica guardado; editado_em acende o selo "editada" */
  texto_original?: string | null;
  falante_original?: Speaker | null;
  editado_por?: string | null;
  editado_em?: string | null;
  /** marco de privacidade (origem "marco"): não é fala */
  marco?: "pausa" | "retomada" | "revogado" | null;
  /** clínica, social ou administrativa (regra; a IA só reclassifica fala sem medicamento, dose, alergia ou alarme) */
  relevancia?: Relevance | null;
  relevancia_origem?: "regra" | "ia";
}
export type Relevance = "clinica" | "social" | "administrativa";

/** Cobertura da nota: falas clínicas que alguma seção cita × as que ficaram de fora. */
export interface NoteCoverage {
  total: number;
  citadas: number;
  percentual: number;
  rotulo: string;
  sociais: number;
  fora: { n: number; ref: string; falante: Speaker; t: number; texto: string }[];
}

/** campos comuns aos insights que o profissional aceita, descarta ou fixa */
export interface InsightItem {
  item_key?: string;
  /** nº de falas analisadas quando o item apareceu (0 = já estava no prontuário) */
  primeira_fala?: number;
  fixado?: boolean;
  acao?: string;
}
export type InsightAction = "aceitar" | "descartar" | "feita" | "nao_se_aplica" | "fixar";
export interface ConsultTag extends InsightItem {
  rotulo: string;
  status: "presente" | "negado";
  refs: string[];
  origem: Origin;
  /** o que já se sabe da queixa pela semiologia ("3 sem · ao levantar") */
  detalhe?: string;
}
export interface ConsultEmotion extends InsightItem {
  emocao: string;
  rotulo: string;
  quem: Speaker;
  intensidade: "baixa" | "media" | "alta";
  trecho: string;
  abordagem: string;
  refs: string[];
  origem: Origin;
}
export interface ConsultQuestion extends InsightItem {
  pergunta: string;
  motivo: string;
  prioridade: Priority;
  /** sugerida: o que motiva a pergunta; abordada: a fala do profissional que a fez */
  refs: string[];
  origem: Origin;
  id?: string;
  /** diretriz de origem da pergunta */
  fonte?: string;
  /** abordada: o que o paciente respondeu e a fala da resposta */
  resposta?: string;
  resposta_refs?: string[];
  /** hipóteses de maior confiança que a resposta ajuda a separar */
  discrimina?: string[];
}
export interface Evidence {
  texto: string;
  refs: string[];
}
export interface ConsultHypothesis extends InsightItem {
  hipotese: string;
  cid: string;
  confianca: Confidence;
  a_favor: Evidence[];
  contra: Evidence[];
  proximos_passos: string;
  sinais_de_alarme: string;
  refs: string[];
  origem: Origin;
  /** descrição oficial do CID-10 (DATASUS) */
  cid_descricao?: string;
  /** a pergunta e o exame que mais separam esta hipótese das vizinhas */
  pergunta_discriminante?: string;
  exame_discriminante?: string;
  fonte?: string;
  /** decisão do profissional: confirmada e provisória viram proposta de Condition; descartada vira negativa pertinente */
  decisao?: HypothesisDecision;
  decisao_motivo?: string;
}
export type HypothesisDecision = "pendente" | "confirmada" | "provisoria" | "descartada";
export interface ProposedCondition {
  codigo: string;
  descricao: string;
  hipotese: string;
  verificacao: "confirmed" | "provisional";
  motivo: string;
  refs: string[];
}
/** Conciliação do que o paciente disse com o prontuário: no prontuário, só relatado ou divergente. */
export type Conciliation = "prontuario" | "relatado_nao_registrado" | "diverge";
export interface HistoryItem {
  texto: string;
  quando: string | null;
  tipo: "diagnostico" | "cirurgia" | "internacao" | "pronto_socorro" | "consulta" | "familiar" | "relatado";
  refs: string[];
  origem: Origin;
  /** antecedente ou cirurgia dito na conversa (estruturado) */
  condicao?: string;
  procedimento?: string;
  cid?: string;
  inicio_aprox?: string | null;
  ano?: number | null;
  intercorrencia?: string;
  conciliacao?: Conciliation | null;
  observacao?: string;
}
export interface AllergyInfo {
  alergeno: string;
  reacao: string;
  fonte: "prontuario" | "relatado";
  reatividade_cruzada: string;
  evitar: string;
  alternativas: string;
  dicas: string;
  conflitos: string[];
  refs: string[];
  origem: Origin;
  chave?: string;
  /** criticidade FHIR: potencial de reação grave */
  criticidade?: "alta" | "baixa" | "indeterminada";
  /** de onde vem a reatividade cruzada */
  fonte_regra?: string;
}
export interface ExamInfo {
  exame: string;
  chave: string;
  valor: string;
  data: string;
  status: "baixo" | "normal" | "alto";
  explicacao: string;
  tendencia: string | null;
  refs: string[];
  origem: Origin;
  /** "relatado (fala 7)" quando o valor veio da conversa */
  origem_dado?: string;
  comparacao?: string;
  conciliacao?: Conciliation | null;
  valor_num?: number | null;
}
export interface MedItem {
  medicamento: string;
  posologia: string;
  desde: string | null;
  status: "em_uso" | "suspenso" | "relatado" | "planejado" | "proposto";
  observacao: string | null;
  refs: string[];
}
export interface MedSuggestion extends InsightItem {
  texto: string;
  tipo:
    | "ajuste_dose"
    | "alternativa"
    | "suspender"
    | "interacao"
    | "registrar"
    | "adesao"
    | "alergia"
    | "efeito_adverso"
    | "alto_alerta";
  severidade: Severity;
  refs: string[];
  origem: Origin;
  /** regra de onde vem o alerta */
  fonte?: string;
}
export interface SurgeryItem {
  texto: string;
  tipo: "historico" | "planejada" | "checagem";
  refs: string[];
  origem: Origin;
}
export interface RcriItem {
  criterio: string;
  pontos: number;
  presente: boolean;
  evidencia: string;
  refs: string[];
  sem_dado: boolean;
}
/** Índice de risco cardíaco revisado (Lee) da cirurgia planejada, com a fonte de cada ponto. */
export interface Rcri {
  cirurgia: string;
  pontos: number;
  classe: string;
  risco_pct: number;
  interpretacao: string;
  itens: RcriItem[];
  fonte: string;
  refs: string[];
}
export interface Reminder {
  chave: string;
  tipo: "alergia" | "protocolo" | "sinais_vitais" | "pos_alta";
  texto: string;
  refs: string[];
  severidade: Severity;
  fonte: string;
}
export interface SummarySentence {
  texto: string;
  refs: string[];
  /** não estava no resumo anterior */
  nova: boolean;
  chave: string;
  origem: Origin;
}
export interface ProtocolItem {
  protocolo: string;
  item: string;
  status: "em_dia" | "pendente" | "sem_registro" | "fora_do_alvo" | "no_alvo" | "abordado" | "critico";
  ultimo: string | null;
  orientacao: string;
  refs: string[];
  id?: string;
  eixo?: string;
  /** "Órgão, documento, ano, página" */
  fonte?: string;
  intervalo_dias?: number;
  /** Item trocado ou criado pelo arquivo local da instituição. */
  local?: boolean;
  pergunta?: string;
}
export interface Highlight extends InsightItem {
  texto: string;
  severidade: Severity;
  refs: string[];
  categoria?: string | null;
}
export type SemioField = "inicio_duracao" | "localizacao" | "intensidade" | "piora" | "melhora" | "frequencia" | "associados";
export interface CampoSemio {
  valor: string;
  refs: string[];
}
export interface QueixaDetalhada extends Record<SemioField | "nega", CampoSemio> {
  chave: string;
  rotulo: string;
  refs: string[];
  /** campos que ainda não foram ditos nem perguntados */
  faltam: SemioField[];
}
export interface CampoEspecialidade {
  campo: string;
  rotulo: string;
  valor: string;
  refs: string[];
}
export interface Anamnese {
  queixa_principal: string;
  hda: string;
  antecedentes: string[];
  medicacoes_em_uso: string[];
  alergias: string[];
  habitos_e_contexto: string[];
  revisao_de_sistemas: string[];
  refs: string[];
  /** chave da queixa principal: a primeira dita ou a que o profissional fixou */
  qp_principal?: string;
  qp_fixada?: boolean;
  queixas_detalhadas?: QueixaDetalhada[];
  campos_especialidade?: CampoEspecialidade[];
}
export interface ReportField {
  campo: string;
  rotulo: string;
  texto: string;
  refs: string[];
  obrigatorio: boolean;
  motivo?: string | null;
  fonte?: string | null;
}
export type OpmeStatus =
  | "conferido"
  | "nao_autorizado"
  | "nao_citado"
  | "quantidade_divergente"
  | "validade_vencida"
  | "sem_etiqueta"
  | "sem_fala";
export type OpmeVia = "ok" | "divergente" | "ausente" | "nao_registrado" | "nao_aplica";
/** Leitura de etiqueta GS1 de um item consumido (lote, validade, série). */
export interface OpmeLote {
  id: string;
  gtin: string | null;
  lote: string | null;
  validade: string | null;
  serie: string | null;
  quantidade: number;
  vencido: boolean;
  registro_anvisa: string | null;
  lido_em: string | null;
  por: string | null;
}
/** Conciliação em três vias: narrado (Fala/N), autorizado (guia) e consumido (etiqueta). */
export interface OpmeLine {
  codigo: string;
  descricao: string;
  autorizado: number;
  citado: number;
  consumido: number;
  status: OpmeStatus;
  refs: string[];
  status_fala: OpmeVia;
  status_guia: OpmeVia;
  status_etiqueta: OpmeVia;
  implantavel: boolean;
  tamanhos: string[];
  lotes: OpmeLote[];
  registro_anvisa: string | null;
  justificativa: string | null;
  editado: boolean;
}
export interface BillingItem {
  regra: string;
  tipo: string;
  severidade: Severity;
  texto: string;
  fonte: string;
  campo: string | null;
  refs: string[];
  peso: number;
}
/** Prontidão para faturamento (0–100) segundo as regras de glosa em dados (data/reference/glosa_rules). */
export interface Readiness {
  score: number;
  pronto: boolean;
  convenio: string;
  convenio_rotulo: string;
  regras_avaliadas: number;
  regras_ok: number;
  criticas: number;
  atencoes: number;
  pendencias: BillingItem[];
  procedimento: string | null;
  codigo_demo: string | null;
  numero_guia: string | null;
  versao: string;
}
export interface OpmeCatalogItem {
  codigo: string;
  descricao: string;
  implantavel: boolean;
  registro_anvisa: string | null;
  gtin: string | null;
}
export interface ConvenioOption {
  value: string;
  label: string;
  codigos: string;
}
export interface OpmeGuideInfo {
  origem: "manual" | "claimresponse" | "tiss";
  numero: string | null;
  senha: string | null;
  itens: number;
  por: string;
  em: string;
}
export interface OpmeScanResult {
  insights: ConsultInsights;
  leitura: {
    id: string;
    codigo: string;
    descricao: string;
    gtin: string | null;
    lote: string | null;
    serie: string | null;
    validade: string | null;
    vencido: boolean;
    avisos: string[];
    registro_anvisa: string | null;
  };
}
export interface CidSuggestion {
  codigo: string;
  descricao: string;
  tipo: "principal" | "secundario" | "hipotese";
  refs: string[];
  /** a fala (ou o registro do prontuário) que justifica o código; some no expurgo da conversa */
  frase?: string;
  /** códigos mais específicos que o ditado sustenta, cada um com a frase */
  mais_especificos?: { codigo: string; descricao: string; frase: string; refs: string[] }[];
  /** o que falta para fechar a especificidade */
  lacunas?: string[];
  /** CIAP-2 mapeado (APS), ex.: "K86 Hipertensão sem complicações" */
  ciap2?: string;
}
export interface ProcedimentoSus {
  codigo: string;
  descricao: string;
  compativel: boolean;
  motivo: string;
  competencia: string;
  fonte: string;
  refs: string[];
}
export interface SurgicalTemplateInfo {
  id: string;
  rotulo: string;
  especialidade: string;
  especialidade_rotulo: string;
  versao: string;
  versao_base: string;
  origem: "ditado" | "agendamento" | "especialidade" | "base";
  refs: string[];
}
export type ChecklistStep = "sign_in" | "time_out" | "sign_out";
export interface SafetyCheck {
  chave: string;
  grupo: "cirurgia_segura" | "sitio" | "contagem" | "intraoperatorio" | "profilaxia";
  rotulo: string;
  status: "ok" | "pendente" | "info" | "atencao" | "critico";
  texto: string;
  fonte: string;
  refs: string[];
  marcado_por?: string | null;
  em?: string | null;
}
export interface ProposedOrder {
  tipo: "imagem";
  descricao: string;
  motivo: string;
  refs: string[];
  recurso: Record<string, unknown>;
}
export interface SurgicalReport {
  campos: ReportField[];
  opme: OpmeLine[];
  pendencias: Highlight[];
  cids: CidSuggestion[];
  procedimento_sus?: ProcedimentoSus | null;
  modelo?: SurgicalTemplateInfo | null;
  agendado?: { procedimento: string; sitio: string | null; lado: string | null; refs: string[] } | null;
  verificacoes?: SafetyCheck[];
  pedidos?: ProposedOrder[];
  perioperatorio?: SurgeryItem[];
  prontidao?: Readiness | null;
}
export type OrderCheckType =
  | "alergia"
  | "reatividade_cruzada"
  | "dose_tfg"
  | "dose_idade"
  | "dose_peso"
  | "interacao"
  | "duplicidade"
  | "alto_alerta"
  | "retirada_abrupta"
  | "sem_prescricao"
  | "exame_repetido"
  | "exame_pendente"
  | "item_nao_reconhecido";
export interface OrderCheck {
  tipo: OrderCheckType;
  severidade: Severity;
  texto: string;
  refs: string[];
}
export interface OrderDecision {
  estado: "pendente" | "assinado" | "recusado";
  justificativa: string;
  por: string | null;
  em: string | null;
  editado: boolean;
  aviso?: string;
}
/** Rascunho de MedicationRequest/ServiceRequest tirado da fala (ou sugerido por protocolo/hipótese), já checado. */
export interface OrderDraft {
  id: string;
  tipo: "medicamento" | "exame";
  acao: "iniciar" | "alterar_dose" | "suspender" | "solicitar";
  item: string;
  chave: string;
  dose: string;
  via: string;
  frequencia: string;
  duracao: string;
  dose_anterior: string;
  justificativa: string;
  refs: string[];
  checagens: OrderCheck[];
  gravidade: Severity;
  fonte_pedido: "fala" | "protocolo" | "hipotese";
  origem: Origin;
  loinc: string;
  loinc_nome: string;
  categoria: string;
  preparo: string;
  prescricao_ref: string;
  decisao: OrderDecision;
}
export interface OrderDecisionIn {
  estado: OrderDecision["estado"];
  justificativa?: string;
  dose?: string;
  via?: string;
  frequencia?: string;
  duracao?: string;
}
/** Um item de Smart App do registro (#21): sempre com a fonte (refs). */
export interface AppItem {
  texto: string;
  detalhe: string;
  severidade: "critico" | "atencao" | "info" | "ok";
  refs: string[];
  tags: string[];
  celulas: string[];
  quando: string | null;
  origem: Origin;
}
export type AppCardType = "lista" | "tags" | "tabela" | "linha_do_tempo";
export type SmartAppTab = "vivo" | "raciocinio" | "medicamentos" | "prontuario" | "pedidos" | "transicao";
/** Saída de um Smart App do registro: o que a tela precisa para desenhar o card genérico. */
export interface AppResult {
  id: string;
  nome: string;
  aba: SmartAppTab;
  card: AppCardType;
  camada: "regra" | "ia" | "mista";
  versao: string;
  base_versao: string;
  colunas: string[];
  itens: AppItem[];
  vazio: string;
  erro: string;
}
/** Manifest de um Smart App (GET /smart-apps) com a marca do perfil do usuário. */
export interface SmartAppManifest {
  id: string;
  nome: string;
  aba: SmartAppTab;
  ordem: number;
  camada: "regra" | "ia" | "mista";
  versao: string;
  saida: { card: AppCardType; colunas: string[] };
  base: { arquivo: string; versao: string; fonte: string } | null;
  gatilho: { tipos: ConsultKind[]; internado: boolean | null };
  oculto: boolean;
}
export interface SmartAppProfile {
  especialidade: string;
  ocultos: string[];
  origem: "especialidade" | "usuario" | "padrao";
}
export const getSmartApps = (especialidade: string) =>
  api<SmartAppManifest[]>(`/smart-apps?especialidade=${encodeURIComponent(especialidade)}`);
export const getSmartAppProfile = (especialidade: string) =>
  api<SmartAppProfile>(`/smart-apps/perfil?especialidade=${encodeURIComponent(especialidade)}`);
export const saveSmartAppProfile = (especialidade: string, ocultos: string[]) =>
  api<SmartAppProfile>("/smart-apps/perfil", { method: "PUT", json: { especialidade, ocultos } });

export interface ConsultInsights {
  tipo: ConsultKind;
  especialidade: string;
  falas_analisadas: number;
  modo: "regras" | "ia";
  queixas: ConsultTag[];
  emocoes: ConsultEmotion[];
  linha_emocional: { n: number; falante: Speaker; emocoes: string[] }[];
  comunicacao: {
    falas: number;
    palavras_paciente_pct: number;
    palavras_profissional_pct: number;
    perguntas_abertas: number;
    perguntas_fechadas: number;
  } | null;
  perguntas: ConsultQuestion[];
  perguntas_abordadas: ConsultQuestion[];
  hipoteses: ConsultHypothesis[];
  historico: HistoryItem[];
  alergias: AllergyInfo[];
  exames: ExamInfo[];
  medicamentos: MedItem[];
  sugestoes_medicamentos: MedSuggestion[];
  cirurgia: SurgeryItem[];
  protocolos: ProtocolItem[];
  resumo: { texto: string; destaques: Highlight[]; frases?: SummarySentence[] } | null;
  anamnese: Anamnese | null;
  /** críticos primeiro; os de atenção vêm depois (a tela os recolhe) */
  alertas: Highlight[];
  lembretes?: Reminder[];
  rcri?: Rcri | null;
  relatorio_cirurgico: SurgicalReport | null;
  pedidos?: OrderDraft[];
  /** "Antes de encaminhar" (#29); ausente em consultas gravadas antes do app */
  encaminhamentos?: Encaminhamento[];
  tfg: number | null;
  descartados: Record<string, unknown>[];
  /** tempo das regras na última fala, em ms */
  regras_ms?: number | null;
  /** itens que o profissional descartou ou marcou como não se aplica */
  ocultos?: number;
  /** Smart Apps do registro (#21); ausente em consultas gravadas antes do registro */
  apps?: AppResult[];
}
export interface CriterioEncaminhamento {
  texto: string;
  /** "confirmar": só o profissional avalia (exame físico, resposta ao tratamento) ou falta dado no prontuário */
  status: "atendido" | "nao_atendido" | "confirmar";
  evidencia: string;
  refs: string[];
}
export interface ExameEncaminhamento {
  exame: string;
  status: "feito" | "desatualizado" | "faltando";
  ultimo: string;
  refs: string[];
}
export type EscolhaEncaminhamento = "pendente" | "encaminhar" | "manejar_aps";
/** Card "Antes de encaminhar": critérios, exames mínimos, manejo na APS e o ServiceRequest em rascunho. */
export interface Encaminhamento {
  id: string;
  especialidade: string;
  condicao: string;
  gatilho: "fala" | "hipotese";
  gatilho_texto: string;
  criterios: CriterioEncaminhamento[];
  exames: ExameEncaminhamento[];
  exames_faltantes: string[];
  manejo_aps: string;
  sinais_urgencia: string;
  teleconsultoria: string;
  fonte: string;
  recomendacao: string;
  refs: string[];
  decisao: { escolha: EscolhaEncaminhamento; motivo: string; por: string | null; em: string | null };
  rascunho: Record<string, unknown>;
}
/** Uma frase da seção com a fonte: refs e quem a escreveu (ia, regra ou profissional). */
export interface NoteSentence {
  texto: string;
  refs: string[];
  origem: "ia" | "regra" | "profissional";
}
export interface NoteSection {
  texto: string;
  refs: string[];
  /** proveniência do texto, frase a frase (o servidor reconcilia com a edição) */
  frases?: NoteSentence[];
}
export type ChronoKind = "queixa" | "negativa" | "achado" | "medicamento" | "exame" | "orientacao" | "retorno" | "emocao";
export interface ChronoItem {
  quando: string;
  texto: string;
  refs: string[];
  tipo?: ChronoKind;
  falante?: string | null;
}
/** Diff por seção entre o rascunho da IA (imutável) e a nota atual. */
export interface NoteDiffSection {
  secao: string;
  rotulo: string;
  pct_editado: number;
  palavras_alteradas: number;
  antes: string;
  depois: string;
}
export interface NoteDiff {
  consulta_id: number;
  editado_pct: number;
  palavras_alteradas: number;
  revisada_por: string | null;
  revisada_em: string | null;
  atestacao: boolean;
  secoes: NoteDiffSection[];
}
export interface CidOption {
  codigo: string;
  descricao: string;
  tipo: string;
}
/** Seção de um modelo de nota (data/reference/note_templates): rótulo, obrigatoriedade, dica e o LOINC da seção. */
export interface NoteSectionDef {
  key: string;
  rotulo: string;
  soap: "S" | "O" | "A" | "P";
  obrigatoria: boolean;
  dica: string;
  loinc?: string | null;
}
/** Modelo de nota com que a nota foi gerada (cópia versionada guardada na própria nota). */
export interface NoteModel {
  id: string;
  versao: string;
  rotulo: string;
  titulo: string;
  tipo: "consulta" | "visita" | "enfermagem" | "ps" | "aps";
  loinc: { codigo: string; display: string };
  secoes: NoteSectionDef[];
}
export interface NoteTemplateInfo extends NoteModel {
  fonte: string;
  especialidades: string[];
  sugerido: boolean;
}
export interface ConsultNote {
  /** modelo da nota (seções, obrigatoriedade e LOINC); o servidor guarda e devolve */
  modelo?: NoteModel;
  /** seções do modelo que não são as nove clássicas (peso, vacinas, CIAP-2...), pela `key` do modelo */
  secoes?: { key: string; texto: string; refs: string[] }[];
  queixa_principal: NoteSection;
  hda: NoteSection;
  antecedentes: NoteSection;
  medicacoes: NoteSection;
  alergias: NoteSection;
  exame_fisico: NoteSection;
  exames_complementares: NoteSection;
  avaliacao: NoteSection;
  plano: NoteSection;
  cids: CidSuggestion[];
  cronologia: ChronoItem[];
  pendencias: NoteSection[];
  orientacoes_paciente: string[];
  /** propostas de Condition das hipóteses confirmadas ou provisórias (o servidor as mantém) */
  condicoes_propostas?: ProposedCondition[];
  /** o que o copiloto inseriu na nota (origem "copiloto"), com as refs; o servidor mantém na revisão */
  copiloto?: CopilotInsertion[];
}

export interface CopilotPhrase {
  texto: string;
  refs: string[];
  fonte: string;
  origem: "regra" | "ia";
}
export interface CopilotAnswer {
  intencao: string;
  modo: "regras" | "ia";
  frases: CopilotPhrase[];
  aviso: string;
  descartados: Record<string, unknown>[];
  acao: { tipo: "plano" | "pendencia"; insercao: string } | null;
}
export interface CopilotMessage {
  id: number;
  pergunta: string;
  via: "texto" | "voz";
  em: string;
  resposta: CopilotAnswer;
}
export interface CopilotInsertion {
  id: string;
  mensagem_id?: number;
  destino: string;
  texto: string;
  refs: string[];
  origem: "copiloto";
  em: string;
}
export interface CopilotState {
  mensagens: CopilotMessage[];
  insercoes: CopilotInsertion[];
  sugestoes: { alerta: string; pergunta: string }[];
  llm_mode: string;
}
export interface Proposal {
  id: string;
  tipo: "alergia" | "medicamento" | "condicao" | "observacao";
  tipo_rotulo: string;
  rotulo: string;
  detalhe: string;
  refs: string[];
  aceita: boolean;
}
export interface SendStatus {
  status: "pendente" | "enviando" | "erro" | "enviado";
  tentativas: number;
  ultimo_erro: string | null;
  atualizado_em: string;
  enviado_em: string | null;
}
export interface Consultation {
  id: number;
  patient_id: string;
  encounter_id: string | null;
  tipo: ConsultKind;
  especialidade: string;
  especialidade_rotulo: string;
  idioma_paciente: string;
  status: "em_andamento" | "finalizada" | "enviada";
  /** cenário sintético do sandbox: fora do plano, nunca vai ao prontuário */
  sandbox?: boolean;
  segmentos: Segment[];
  consentimento?: ConsentInfo | null;
  /** propostas para o prontuário (alergia/medicamento relatados, CID, PA e peso ditos); aceite item a item */
  propostas?: Proposal[];
  /** fila de envio ao prontuário (null: nunca enviada) */
  envio?: SendStatus | null;
  insights: ConsultInsights | null;
  ia: {
    modo: "real" | "mock";
    modelo: string;
    prompt_version: string;
    falas: number;
    em: string;
    segundos: number;
    descartados: Record<string, unknown>[];
  } | null;
  nota: (ConsultNote & { relatorio_cirurgico?: undefined }) | { relatorio_cirurgico: SurgicalReport } | null;
  /** cobertura da nota calculada na hora (some no centro cirúrgico e antes da nota) */
  cobertura?: NoteCoverage | null;
  nota_meta: {
    modo: string;
    modelo: string;
    prompt_version: string;
    gerada_em: string;
    /** itens da IA descartados por falta de fonte válida (seção, texto e motivo) */
    descartados?: Record<string, unknown>[];
    cobertura?: NoteCoverage | null;
    revisada_por: string | null;
    revisada_em: string | null;
    /** paciente recusou a IA externa: nota só das regras */
    sem_ia?: boolean;
    /** a transcrição foi editada depois da nota: seções que citam as falas editadas */
    desatualizada?: { falas: number[]; secoes: string[]; em: string; por: string } | null;
    /** "Revisei e assumo a responsabilidade": sem isso o servidor recusa exportar e enviar */
    atestacao?: boolean;
    /** nota montada sem a IA ("IA indisponível") */
    aviso?: string | null;
    versoes?: { n: number; por: string; em: string; editado_pct: number; atestada: boolean; secoes: Record<string, number> }[];
  } | null;
  fhir_ref: string | null;
  roteiro_demo: string | null;
  /** painel "Privacidade desta consulta": gerado da configuração em vigor */
  privacidade?: PrivacyPanel & { expurgada_em: string | null };
  opme_guia?: OpmeGuideInfo | null;
  faturamento?: { convenio?: string | null; numero_guia?: string | null } | null;
  justificativa_envio?: { texto: string; por: string; em: string; regras_justificadas: string[] } | null;
  pedidos_fhir?: string[] | null;
  criado_por: string | null;
  created_at: string;
  finished_at: string | null;
  sent_at: string | null;
}
export interface ConsentPurposes {
  transcricao: boolean;
  ia_externa: boolean;
  traducao: boolean;
  emocoes: boolean;
}
export interface ConsentInput {
  quem: "paciente" | "acompanhante" | "responsavel";
  related_person_id?: string | null;
  finalidades: ConsentPurposes;
  recusou: boolean;
}
export interface ConsentInfo extends Omit<ConsentInput, "related_person_id"> {
  quem_nome: string | null;
  related_person_id: string | null;
  estado: "ativo" | "pausado" | "revogado";
  em: string;
  por: string;
  fhir_ref: string | null;
  permite: { ia: boolean; traducao: boolean; transcricao_servidor: boolean; emocoes: boolean };
}

export interface PrivacyPanel {
  guardado: string;
  retencao: string;
  retencao_dias: number | null;
  voz: string;
  ia: string;
  criptografia: string;
  criptografia_ativa: boolean;
  stt_navegador: boolean;
  exclusao: string;
}

export interface AmbienteItem {
  id: string;
  titulo: string;
  estado: "ok" | "atencao" | "info";
  texto: string;
}
export interface Ambiente {
  env: "development" | "production";
  producao: boolean;
  llm_mode: "real" | "mock";
  llm: { provedor: string | null; modelo: string | null; regiao: string | null };
  fhir_mode: string;
  checklist: AmbienteItem[];
}

export interface ConsultConfig {
  aviso_consentimento: Record<string, string>;
  finalidades: { value: keyof ConsentPurposes; label: string }[];
  llm_mode: "real" | "mock";
  stt_servidor: boolean;
  /** o servidor aceita áudio em fluxo (WebSocket) com texto parcial; senão cai nos trechos de 8 s */
  stt_streaming?: boolean;
  /** false (ALLOW_BROWSER_STT): o áudio não vai ao fabricante do navegador e o modo Microfone some */
  stt_navegador: boolean;
  privacidade: PrivacyPanel;
  ambiente: Ambiente;
  envio_prontuario: boolean;
  idiomas: { value: string; label: string; web_speech: string; whisper: string }[];
  especialidades: { value: string; label: string; foco: string; cirurgica: boolean }[];
}
export interface Appointment {
  encounter_id: string;
  patient_id: string;
  nome: string;
  nascimento: string | null;
  horario: string;
  status_fhir: string;
  servico: string;
  profissional: string;
  motivo: string;
  consulta_id: number | null;
  consulta_status: Consultation["status"] | null;
  envio_status: SendStatus["status"] | null;
  cenario: boolean;
}
export interface DemoScript {
  id: string;
  patient_id: string;
  tipo: ConsultKind;
  especialidade?: string;
  titulo: string;
  idioma_paciente?: string;
  falas: { t: number; falante: Speaker; texto: string; idioma?: string }[];
}

// ---------- Jornada do paciente ----------
export type JourneyRule =
  | "alta_sem_contato_72h"
  | "retorno_nao_agendado"
  | "falta_ao_retorno"
  | "protocolo_aps_vencido"
  | "nao_adesao_consulta"
  | "troca_sem_reconciliacao";

export interface JourneyTask {
  id: number;
  patient_id: string;
  regra: JourneyRule;
  titulo: string;
  detalhe: string;
  papel: Role;
  prazo: string;
  status: "aberta" | "concluida" | "cancelada";
  fontes: string[];
  fhir_ref: string | null;
  criada_em: string;
  encerrada_em: string | null;
  observacao_encerramento: string | null;
  paciente?: { id: string; nome: string };
}

export interface JourneyItem {
  id: string;
  regra: JourneyRule;
  regra_titulo: string;
  severidade: Severity;
  situacao: "vencido" | "a_vencer";
  paciente: {
    id: string;
    nome: string;
    idade: number | null;
    sexo: string;
    telefone: string | null;
    familiar: { nome: string; parentesco: string; telefone: string | null } | null;
  };
  titulo: string;
  detalhe: string;
  fontes: string[];
  fonte_regra: string;
  desde: string;
  prazo: string | null;
  papel_sugerido: Role;
  prazo_tarefa_horas: number;
  tarefa: JourneyTask | null;
  contexto: Record<string, unknown>;
}

export interface JourneyWorklist {
  gerado_em: string;
  resumo: { itens: number; pacientes: number; criticos: number; com_tarefa: number; por_regra: Record<string, number> };
  itens: JourneyItem[];
}

export interface ContactQuestion {
  id: string;
  tipo: "confirmacao" | "teach_back" | "retirada" | "sinal_alarme";
  texto: string;
  alarme: string | null;
  risco_quando: "sim" | "nao";
}

export type ContactAnswer = "sim" | "nao" | "nao_se_aplica";

export interface ContactResult {
  id: number;
  atendeu: boolean;
  canal: string;
  sinais_de_alarme: string[];
  pendencias: { pergunta: string; tipo: string; texto: string }[];
  fhir_ref: string | null;
  alerta: { id: number; titulo: string; severidade: Severity } | null;
  texto: string | null;
}

export interface MessageDraft {
  texto: string;
  envio: false;
  aviso: string;
  telefone: string | null;
  fontes: string[];
}

export interface JourneyMetricsRow {
  altas: number;
  seguimento_completo: number;
  em_seguimento: number;
  reinternacoes: number;
  taxa_reinternacao: number | null;
  reinternados_em_seguimento: number;
  retornos_perdidos: number;
  taxa_retorno_perdido: number | null;
  faltas: number;
}

export interface JourneyCase {
  paciente_id: string;
  paciente: string;
  encounter_ref: string;
  alta_em: string;
  lace: number;
  risco: Risk;
  coorte: string;
  seguimento_completo: boolean;
  reinternado: boolean;
  reinternacao_ref: string | null;
  dias_ate_reinternacao: number | null;
  retorno_realizado: boolean;
  retorno_ref: string | null;
  falta_ref: string | null;
}

export interface JourneyMetrics {
  gerado_em: string;
  parametros: { coorte: string; dias: number; reinternacao_dias: number; retorno_dias: number };
  metodo: string;
  total: JourneyMetricsRow;
  coortes: (JourneyMetricsRow & { chave: string; rotulo: string })[];
  casos: JourneyCase[];
}

// ---------- Modo consultório (#34) ----------
export interface PacienteAvulsoIn {
  nome: string;
  nascimento: string;
  sexo: "M" | "F" | "O";
  telefone?: string;
  alergias: { substancia: string; reacao?: string; criticidade: "alta" | "baixa" | "indeterminada" }[];
  medicamentos: { nome: string; posologia?: string; via?: string }[];
  sem_alergias?: boolean;
}
export interface PacienteAvulso {
  id: string;
  nome: string;
  nascimento: string | null;
  sexo: string;
  avulso: true;
  alergias?: number;
  medicamentos?: number;
}
export interface PlanoUso {
  plano: PlanoResumo["codigo"];
  nome: string;
  limite: number | null;
  usadas: number;
  restantes: number | null;
  sandbox: number;
  aviso: string | null;
  vigencia_ate: string | null;
  vencido: boolean;
  modulos: ModuloHospitalar[];
}
export interface CenarioSandbox {
  id: string;
  patient_id: string;
  paciente: string;
  titulo: string;
  tipo: ConsultKind;
  especialidade: string;
  especialidade_rotulo: string;
  sintetico: true;
}
export interface ConsultaLinha {
  id: number;
  patient_id: string;
  paciente: string;
  tipo: ConsultKind;
  especialidade_rotulo: string;
  status: "em_andamento" | "finalizada" | "enviada";
  revisada: boolean;
  aguardando_revisao: boolean;
  sandbox: boolean;
  cids: { codigo: string; descricao: string }[];
  falas: number;
  criado_por: string | null;
  created_at: string;
  finished_at: string | null;
}
export interface ConsultaBusca {
  total: number;
  limite: number;
  deslocamento: number;
  itens: ConsultaLinha[];
}
export interface OnboardingConsultorio {
  passos: { chave: string; titulo: string; feito: boolean }[];
  concluido: boolean;
}
