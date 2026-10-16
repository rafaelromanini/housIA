"use client";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const TOKEN_KEY = "housia.token";
const USER_KEY = "housia.user";

export type Role = "medico" | "enfermagem" | "gestor";
export type Severity = "info" | "atencao" | "critico";
export type Risk = "baixo" | "moderado" | "alto";

export interface User {
  id: number;
  name: string;
  email: string;
  role: Role;
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

export type EventKind = "vital" | "lab" | "med_order" | "med_admin" | "note" | "procedure" | "transfer" | "diagnosis" | "io";

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

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getStoredUser(): User | null {
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

export function storeSession(token: string, user: User) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    /* armazenamento indisponível: sessão só em memória */
  }
}

export function clearSession() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    /* noop */
  }
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`${API_URL}${path}`, { ...init, headers, body });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/login")) {
    clearSession();
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
  source: "handoff" | "medrec" | "discharge";
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

export interface MedrecRun {
  id: number;
  patient_id: string;
  created_at: string;
  origem: SectorRef | null;
  destino: SectorRef;
  linhas: RecRow[];
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
}

// ---------- Melhorias (benchmark): feedback, conferência verbal, documentos de alta ----------
export type FeedbackKind = "handoff" | "handoff_check" | "medrec" | "discharge" | "discharge_docs" | "consulta" | "consulta_nota";

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
  idioma: "pt-BR" | "es" | "en";
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
  meta: { modo: "mock" | "real"; gerado_em: string; traducao_disponivel: boolean };
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
  origem: "microfone" | "servidor" | "digitado" | "demo";
}

export interface ConsultTag {
  rotulo: string;
  status: "presente" | "negado";
  refs: string[];
  origem: Origin;
}
export interface ConsultEmotion {
  emocao: string;
  rotulo: string;
  quem: Speaker;
  intensidade: "baixa" | "media" | "alta";
  trecho: string;
  abordagem: string;
  refs: string[];
  origem: Origin;
}
export interface ConsultQuestion {
  pergunta: string;
  motivo: string;
  prioridade: Priority;
  refs: string[];
  origem: Origin;
}
export interface Evidence {
  texto: string;
  refs: string[];
}
export interface ConsultHypothesis {
  hipotese: string;
  cid: string;
  confianca: Confidence;
  a_favor: Evidence[];
  contra: Evidence[];
  proximos_passos: string;
  sinais_de_alarme: string;
  refs: string[];
  origem: Origin;
}
export interface HistoryItem {
  texto: string;
  quando: string | null;
  tipo: "diagnostico" | "cirurgia" | "internacao" | "pronto_socorro" | "consulta" | "familiar" | "relatado";
  refs: string[];
  origem: Origin;
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
}
export interface MedItem {
  medicamento: string;
  posologia: string;
  desde: string | null;
  status: "em_uso" | "suspenso" | "relatado";
  observacao: string | null;
  refs: string[];
}
export interface MedSuggestion {
  texto: string;
  tipo: "ajuste_dose" | "alternativa" | "suspender" | "interacao" | "registrar" | "adesao" | "alergia";
  severidade: Severity;
  refs: string[];
  origem: Origin;
}
export interface SurgeryItem {
  texto: string;
  tipo: "historico" | "planejada" | "checagem";
  refs: string[];
  origem: Origin;
}
export interface ProtocolItem {
  protocolo: string;
  item: string;
  status: "em_dia" | "pendente" | "sem_registro" | "fora_do_alvo" | "no_alvo";
  ultimo: string | null;
  orientacao: string;
  refs: string[];
}
export interface Highlight {
  texto: string;
  severidade: Severity;
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
export interface OpmeLine {
  codigo: string;
  descricao: string;
  autorizado: number;
  citado: number;
  status: "conferido" | "nao_autorizado" | "nao_citado" | "quantidade_divergente";
  refs: string[];
}
export interface CidSuggestion {
  codigo: string;
  descricao: string;
  tipo: "principal" | "secundario" | "hipotese";
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
  modelo?: SurgicalTemplateInfo | null;
  agendado?: { procedimento: string; sitio: string | null; lado: string | null; refs: string[] } | null;
  verificacoes?: SafetyCheck[];
  pedidos?: ProposedOrder[];
  perioperatorio?: SurgeryItem[];
}
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
  resumo: { texto: string; destaques: Highlight[] } | null;
  anamnese: Anamnese | null;
  alertas: Highlight[];
  relatorio_cirurgico: SurgicalReport | null;
  tfg: number | null;
  descartados: Record<string, unknown>[];
}
export interface NoteSection {
  texto: string;
  refs: string[];
}
export interface ConsultNote {
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
  cronologia: { quando: string; texto: string; refs: string[] }[];
  pendencias: NoteSection[];
  orientacoes_paciente: string[];
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
  segmentos: Segment[];
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
  nota_meta: {
    modo: string;
    modelo: string;
    prompt_version: string;
    gerada_em: string;
    revisada_por: string | null;
    revisada_em: string | null;
  } | null;
  fhir_ref: string | null;
  roteiro_demo: string | null;
  criado_por: string | null;
  created_at: string;
  finished_at: string | null;
  sent_at: string | null;
}
export interface ConsultConfig {
  llm_mode: "real" | "mock";
  stt_servidor: boolean;
  envio_prontuario: boolean;
  idiomas: { value: string; label: string }[];
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
  cenario: boolean;
}
export interface DemoScript {
  id: string;
  patient_id: string;
  tipo: ConsultKind;
  especialidade?: string;
  titulo: string;
  falas: { t: number; falante: Speaker; texto: string }[];
}
