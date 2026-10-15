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
