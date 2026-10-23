"use client";

import { useEffect, useState } from "react";
import { CircleCheck, ClipboardCopy, Download, FileCheck2, FileText, GitCompareArrows, Loader2, Plus, RefreshCw, Send, ShieldAlert, Trash2 } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { NpsPrompt } from "@/components/ai/nps-prompt";
import { usePatient } from "@/components/patient/patient-context";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { CidEditor } from "@/components/consult/cid-editor";
import { OPME_STATUS } from "@/components/consult/opme-panel";
import { ReadinessCard } from "@/components/consult/readiness-card";
import { OrdersCard } from "@/components/consult/orders-card";
import { DocumentsPanel } from "@/components/consult/documents-panel";
import { ProposalsPanel } from "@/components/consult/proposals-panel";
import { api, downloadFile, ApiError, type ChronoItem, type ConsultNote, type Consultation, type NoteCoverage, type NoteDiff, type NoteSection, type NoteSectionDef, type NoteTemplateInfo, type OpmeLine, type SurgicalReport } from "@/lib/api";
import { focusFala } from "@/components/source-ref";
import { fmtDateTime } from "@/lib/format";

/** As nove seções clássicas moram em campos próprios da nota; as demais do modelo ficam em `secoes`. */
const FIXED_KEYS = ["queixa_principal", "hda", "antecedentes", "medicacoes", "alergias", "exame_fisico", "exames_complementares", "avaliacao", "plano"];

/** Seções usadas quando a nota ainda não traz o modelo (o servidor sempre devolve; isto cobre só respostas antigas em cache). */
const DEFAULT_SECTIONS: NoteSectionDef[] = [
  { key: "queixa_principal", rotulo: "Queixa principal", soap: "S", obrigatoria: false, dica: "" },
  { key: "hda", rotulo: "História da doença atual", soap: "S", obrigatoria: false, dica: "" },
  { key: "antecedentes", rotulo: "Antecedentes", soap: "S", obrigatoria: false, dica: "" },
  { key: "medicacoes", rotulo: "Medicações em uso", soap: "S", obrigatoria: false, dica: "" },
  { key: "alergias", rotulo: "Alergias", soap: "S", obrigatoria: false, dica: "" },
  { key: "exame_fisico", rotulo: "Exame físico", soap: "O", obrigatoria: false, dica: "" },
  { key: "exames_complementares", rotulo: "Exames complementares", soap: "O", obrigatoria: false, dica: "" },
  { key: "avaliacao", rotulo: "Avaliação", soap: "A", obrigatoria: false, dica: "" },
  { key: "plano", rotulo: "Plano", soap: "P", obrigatoria: false, dica: "" },
];

/** Texto e fontes de uma seção do modelo, venha ela de um campo clássico ou de `secoes`. */
function readSection(note: ConsultNote, key: string): NoteSection {
  if (FIXED_KEYS.includes(key)) return (note as unknown as Record<string, NoteSection>)[key] ?? { texto: "", refs: [] };
  return note.secoes?.find((s) => s.key === key) ?? { texto: "", refs: [] };
}

/** Mensagem do 422 de seção obrigatória vazia (ou o texto do erro). */
function apiMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: { mensagem?: string } } | null)?.detail;
    if (detail && typeof detail === "object" && detail.mensagem) return detail.mensagem;
  }
  return e instanceof Error ? e.message : fallback;
}

/** Descartes da IA por falta de fonte válida (o servidor os grava em nota_meta; aparecem aqui como no passagem de plantão). */
function DiscardedItems({ items }: { items: Record<string, unknown>[] }) {
  if (items.length === 0) return null;
  return (
    <Alert variant="info">
      <ShieldAlert aria-hidden />
      <details>
        <summary className="min-h-9 cursor-pointer font-bold">
          {items.length} item(ns) gerado(s) pela IA foram descartados por não citarem uma fonte válida
        </summary>
        <ul className="mt-2 list-disc pl-4 text-sm">
          {items.map((d, i) => (
            <li key={i}>
              {String(d.secao ?? "item")}: {String(d.texto ?? d.cid ?? "")} {d.motivo ? `(${String(d.motivo)})` : ""}
            </li>
          ))}
        </ul>
      </details>
    </Alert>
  );
}

/** "Fora da nota": as falas clínicas que nenhuma seção cita, a cobertura e o botão para incluir cada uma onde o médico quiser. */
function OutsideNote({
  coverage,
  disabled,
  onInclude,
  targets,
}: {
  coverage: NoteCoverage;
  disabled: boolean;
  onInclude: (n: number, destino: string) => Promise<void>;
  targets: { value: string; label: string }[];
}) {
  const [target, setTarget] = useState<Record<number, string>>({});
  const [working, setWorking] = useState<number | null>(null);
  return (
    <section aria-label="Fora da nota" className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-bold">Fora da nota</h3>
        <p className="text-sm tabular-nums" data-testid="cobertura-nota">
          {coverage.rotulo} ({coverage.percentual}%)
        </p>
      </div>
      {coverage.fora.length === 0 ? (
        <p className="text-sm text-muted-foreground">Todas as falas clínicas estão citadas na nota.</p>
      ) : (
        <ul className="space-y-2">
          {coverage.fora.map((f) => {
            const destino = target[f.n] ?? "avaliacao";
            return (
              <li key={f.n} className="flex flex-wrap items-center gap-2 rounded-md bg-secondary/50 p-2 text-sm">
                <span className="min-w-0 flex-1 break-words">
                  <span className="mr-1 font-mono text-xs text-muted-foreground">fala {f.n}</span>
                  {f.texto}
                </span>
                {!disabled && (
                  <span className="flex items-center gap-1.5">
                    <NativeSelect
                      aria-label={`Seção para incluir a fala ${f.n}`}
                      value={destino}
                      onChange={(e) => setTarget((t) => ({ ...t, [f.n]: e.target.value }))}
                      className="h-9 w-44"
                    >
                      {targets.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </NativeSelect>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={working !== null}
                      aria-label={`Incluir a fala ${f.n} na nota`}
                      onClick={async () => {
                        setWorking(f.n);
                        try {
                          await onInclude(f.n, destino);
                        } finally {
                          setWorking(null);
                        }
                      }}
                    >
                      Incluir em…
                    </Button>
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {coverage.sociais > 0 && (
        <p className="text-xs text-muted-foreground">{coverage.sociais} fala(s) social(is) ou administrativa(s) não entram na conta.</p>
      )}
    </section>
  );
}

type ExportOut = {
  texto: string;
  recurso: Record<string, unknown>;
  pedidos?: Record<string, unknown>[];
  /** Bundle de transação (cirurgia): relatório, pedidos propostos, implantes (Device), Procedure e Claim em rascunho. */
  bundle?: Record<string, unknown> | null;
};

/** Mínimo de caracteres da justificativa: pendência de faturamento (cirurgia) 15; pendência crítica de segurança 10. */
const MIN_JUSTIFICATION = { prontidao_critica: 15, pendencia_critica: 10 } as const;
type BlockCode = keyof typeof MIN_JUSTIFICATION;
type Blocked = { codigo: BlockCode; mensagem: string; pendencias: { texto: string; fonte?: string; refs?: string[] }[] };

/** 409 da trava de envio: pendência crítica (faturamento ou segurança) sem justificativa. */
function blockedBy(e: unknown): Blocked | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = (e.body as { detail?: { codigo?: string; mensagem?: string; pendencias?: Blocked["pendencias"] } } | null)?.detail;
  if (detail?.codigo !== "prontidao_critica" && detail?.codigo !== "pendencia_critica") return null;
  return { codigo: detail.codigo, mensagem: detail.mensagem ?? "", pendencias: detail.pendencias ?? [] };
}

const PLACEHOLDER = /^sem registro na consulta/i;
const ORIGEM_LABEL = { ia: "IA", regra: "regra", profissional: "profissional" } as const;

/** Seção sem nenhuma ref (nem nas frases) e com texto: o profissional vê o aviso "sem fonte" antes de assinar. */
function semFonte(section: NoteSection): boolean {
  const text = section.texto.trim();
  if (!text || PLACEHOLDER.test(text)) return false;
  return section.refs.length === 0 && !(section.frases ?? []).some((f) => f.refs.length > 0);
}

/** Frases de uma seção com a fonte de cada uma: clicar leva até a fala citada na transcrição. */
function SentenceSources({
  section,
  patientId,
  onPick,
}: {
  section: NoteSection;
  patientId: string;
  onPick: (refs: string[]) => void;
}) {
  const frases = section.frases ?? [];
  if (frases.length === 0) return null;
  return (
    <details className="text-xs">
      <summary className="min-h-9 cursor-pointer text-muted-foreground">Fonte de cada frase ({frases.length})</summary>
      <ul className="mt-1 space-y-1">
        {frases.map((f, i) => (
          <li key={i} className="flex flex-wrap items-start gap-2 rounded-md bg-secondary/40 p-1.5">
            <Badge variant={f.origem === "profissional" ? "ok" : f.origem === "ia" ? "ai" : "outline"} className="text-[10px]">
              {ORIGEM_LABEL[f.origem]}
            </Badge>
            <button
              type="button"
              className="min-w-0 flex-1 text-left text-sm underline-offset-2 hover:underline"
              onClick={() => onPick(f.refs)}
              aria-label={`Destacar a fonte da frase: ${f.texto.slice(0, 60)}`}
            >
              {f.texto}
            </button>
            {f.refs.length > 0 ? <SourceRefs patientId={patientId} refs={f.refs} /> : <span className="text-muted-foreground">sem ref (escrita pelo profissional)</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

/** Quanto o profissional mudou em cada seção em relação ao rascunho da IA (a prova da supervisão humana). */
function DiffPanel({ diff }: { diff: NoteDiff }) {
  return (
    <section aria-label="Comparação com o rascunho da IA" className="space-y-2 rounded-md border p-3" data-testid="painel-diff">
      <p className="text-sm font-bold">
        Editado em relação ao rascunho da IA: <span className="tabular-nums">{diff.editado_pct}%</span> ({diff.palavras_alteradas} palavra(s))
      </p>
      <ul className="space-y-1 text-sm">
        {diff.secoes.map((d) => (
          <li key={d.secao}>
            <details>
              <summary className="flex min-h-9 cursor-pointer flex-wrap items-center gap-2">
                <span className="font-bold">{d.rotulo}</span>
                <Badge variant={d.pct_editado > 0 ? "atencao" : "outline"} className="tabular-nums">
                  {d.pct_editado}% editado
                </Badge>
              </summary>
              <div className="mt-1 grid gap-2 sm:grid-cols-2">
                <p className="rounded bg-secondary/50 p-2">
                  <span className="block text-xs font-bold text-muted-foreground">Rascunho da IA</span>
                  {d.antes || "—"}
                </p>
                <p className="rounded bg-secondary/50 p-2">
                  <span className="block text-xs font-bold text-muted-foreground">Nota revisada</span>
                  {d.depois || "—"}
                </p>
              </div>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Revisão obrigatória: o profissional edita, atesta e só então a nota vai para o prontuário (o servidor confere). */
export function NoteReview({
  consultation: c,
  onChange,
  canSend,
}: {
  consultation: Consultation;
  onChange: (c: Consultation) => void;
  canSend: boolean;
}) {
  const { patient } = usePatient();
  const r = (t: string) => reidentify(t, patient.nome);
  const surgical = c.tipo === "cirurgia";
  const [draft, setDraft] = useState(() => structuredClone(c.nota) as Record<string, unknown>);
  const [busy, setBusy] = useState<"save" | "send" | "copy" | "download" | "pdf" | "recompute" | "diff" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sent = c.status === "enviada";
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [justification, setJustification] = useState("");
  const [attested, setAttested] = useState(Boolean(c.nota_meta?.atestacao));
  const [diff, setDiff] = useState<NoteDiff | null>(null);

  const note = draft as unknown as ConsultNote;
  const report = (draft as { relatorio_cirurgico?: SurgicalReport }).relatorio_cirurgico;
  // aprovada no servidor = revisada + atestada, e o que está na tela é o que foi salvo (copiar/baixar usam o servidor)
  const approved = Boolean(c.nota_meta?.revisada_por && c.nota_meta?.atestacao);
  const dirty = JSON.stringify(draft) !== JSON.stringify(c.nota);

  const sections = note.modelo?.secoes ?? DEFAULT_SECTIONS;
  const includeTargets = [
    ...sections.filter((s) => FIXED_KEYS.includes(s.key)).map((s) => ({ value: s.key, label: s.rotulo })),
    { value: "pendencia", label: "Pendências" },
  ];
  const [templates, setTemplates] = useState<NoteTemplateInfo[]>([]);
  useEffect(() => {
    if (surgical) return;
    let cancelled = false;
    api<NoteTemplateInfo[]>(`/note-templates?especialidade=${encodeURIComponent(c.especialidade)}&tipo=${encodeURIComponent(c.tipo)}`)
      .then((list) => !cancelled && setTemplates(list))
      .catch(() => undefined); // sem a lista, a nota continua editável com o modelo que já tem
    return () => {
      cancelled = true;
    };
  }, [c.especialidade, c.tipo, surgical]);

  function setSection(key: string, texto: string) {
    if (FIXED_KEYS.includes(key)) {
      setDraft((d) => ({ ...d, [key]: { ...(d[key] as object), texto } }));
      return;
    }
    setDraft((d) => {
      const list = ((d as { secoes?: { key: string; texto: string; refs: string[] }[] }).secoes ?? []).slice();
      const i = list.findIndex((s) => s.key === key);
      if (i >= 0) list[i] = { ...list[i], texto };
      else list.push({ key, texto, refs: [] });
      return { ...d, secoes: list };
    });
  }

  /** Troca o modelo na revisão: o servidor completa as seções novas; o que já foi escrito aqui continua valendo. */
  async function switchTemplate(id: string) {
    setError(null);
    try {
      const updated = await api<Consultation>(`/consultations/${c.id}/note-template`, { method: "PUT", json: { modelo: id } });
      onChange(updated);
      const fresh = structuredClone(updated.nota) as unknown as ConsultNote;
      const old = draft as unknown as ConsultNote;
      for (const def of fresh.modelo?.secoes ?? []) {
        const mine = readSection(old, def.key);
        if (mine.texto.trim()) {
          if (FIXED_KEYS.includes(def.key)) (fresh as unknown as Record<string, unknown>)[def.key] = mine;
          else fresh.secoes = (fresh.secoes ?? []).map((s) => (s.key === def.key ? { ...s, texto: mine.texto, refs: mine.refs } : s));
        }
      }
      setDraft(fresh as unknown as Record<string, unknown>);
      setMessage("Modelo da nota trocado: confira as seções novas.");
    } catch (e) {
      setError(apiMessage(e, "Não foi possível trocar o modelo"));
    }
  }
  function patchReport(fn: (rep: SurgicalReport) => void) {
    setDraft((d) => {
      const rep = structuredClone((d as { relatorio_cirurgico: SurgicalReport }).relatorio_cirurgico);
      fn(rep);
      return { ...d, relatorio_cirurgico: rep };
    });
  }
  function setOpme(code: string, patch: Partial<OpmeLine> | null) {
    patchReport((rep) => {
      rep.opme = patch === null ? rep.opme.filter((o) => o.codigo !== code) : rep.opme.map((o) => (o.codigo === code ? { ...o, ...patch } : o));
    });
  }
  function setField(i: number, texto: string) {
    patchReport((rep) => {
      rep.campos[i].texto = texto;
    });
  }
  function setList<K extends "orientacoes_paciente" | "cronologia">(key: K, items: ConsultNote[K]) {
    setDraft((d) => ({ ...d, [key]: items }));
  }

  /** Clicar numa frase leva até a fala na transcrição ao lado da nota (a página cuida da aba no celular). */
  function pickSource(refs: string[]) {
    const falas = refs.filter((x) => x.startsWith("Fala/")).map((x) => Number(x.split("/")[1]));
    if (falas.length === 0) return;
    window.setTimeout(() => focusFala(falas[0]), 80);
  }

  async function save(attest = attested): Promise<Consultation | null> {
    setBusy("save");
    setError(null);
    try {
      const updated = await api<Consultation>(`/consultations/${c.id}/note`, { method: "PUT", json: { ...draft, atestacao: attest } });
      onChange(updated);
      // o servidor recalcula conciliação de OPME, pendências, prontidão e a origem das frases: o rascunho mostra o que ele devolveu
      if (updated.nota) setDraft(structuredClone(updated.nota) as Record<string, unknown>);
      setMessage(attest ? "Revisão salva e atestada." : "Revisão salva (sem atestação: ainda não pode enviar).");
      return updated;
    } catch (e) {
      setError(apiMessage(e, "Erro ao salvar"));
      return null;
    } finally {
      setBusy(null);
    }
  }

  /** Recalcula OPME, pendências e prontidão do relatório cirúrgico editado (não salva a nota). */
  async function recompute() {
    setBusy("recompute");
    setError(null);
    try {
      const out = await api<{ relatorio_cirurgico: SurgicalReport }>(`/consultations/${c.id}/surgical/recompute`, {
        method: "POST",
        json: { relatorio_cirurgico: report },
      });
      patchReport((rep) => {
        rep.opme = out.relatorio_cirurgico.opme;
        rep.pendencias = out.relatorio_cirurgico.pendencias;
        rep.prontidao = out.relatorio_cirurgico.prontidao;
      });
      setMessage("Pendências e prontidão recalculadas com o que está na tela.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível recalcular");
    } finally {
      setBusy(null);
    }
  }

  async function loadDiff() {
    setBusy("diff");
    setError(null);
    try {
      setDiff(await api<NoteDiff>(`/consultations/${c.id}/note/diff`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível comparar");
    } finally {
      setBusy(null);
    }
  }

  /** "Incluir em…": o servidor acrescenta a fala (com a ref) à seção; o rascunho recebe só essa seção, sem perder edições. */
  async function include(n: number, destino: string) {
    setError(null);
    try {
      const out = await api<{ nota: Record<string, unknown>; cobertura: NoteCoverage; incluidas: unknown[] }>(
        `/consultations/${c.id}/nota/incluir`,
        { method: "POST", json: { n, destino } },
      );
      setDraft((d) => (destino === "pendencia" ? { ...d, pendencias: out.nota.pendencias } : { ...d, [destino]: out.nota[destino] }));
      onChange({ ...c, nota: out.nota as unknown as Consultation["nota"], cobertura: out.cobertura, nota_meta: c.nota_meta ? { ...c.nota_meta, atestacao: false } : c.nota_meta });
      setAttested(false);
      setMessage(`Fala ${n} incluída na nota. Atestar de novo ao salvar a revisão.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível incluir a fala");
    }
  }

  async function exportOut(): Promise<ExportOut> {
    return api<ExportOut>(`/consultations/${c.id}/export`);
  }

  // copiar e baixar leem a nota JÁ aprovada no servidor: não salvam nada por baixo (salvar é aprovar)
  async function copy() {
    setBusy("copy");
    setError(null);
    try {
      const out = await exportOut();
      await navigator.clipboard.writeText(out.texto);
      setMessage("Texto copiado: cole no prontuário.");
    } catch (e) {
      const block = blockedBy(e); // pendência crítica: o mesmo painel de justificativa do envio
      if (block) setBlocked(block);
      else setError(e instanceof Error ? e.message : "Não foi possível copiar");
    } finally {
      setBusy(null);
    }
  }

  async function download() {
    setBusy("download");
    setError(null);
    try {
      const out = await exportOut();
      // cirurgia: Bundle de transação (relatório, pedidos, implantes, Procedure e Claim em rascunho);
      // com pedidos propostos e sem Bundle, uma coleção: relatório + ServiceRequest(s)
      const fhir = out.bundle
        ? out.bundle
        : out.pedidos?.length
        ? { resourceType: "Bundle", type: "collection", entry: [out.recurso, ...out.pedidos].map((resource) => ({ resource })) }
        : out.recurso;
      const blob = new Blob([JSON.stringify(fhir, null, 2)], { type: "application/fhir+json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `nota-consulta-${c.id}.fhir.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      const block = blockedBy(e);
      if (block) setBlocked(block);
      else setError(e instanceof Error ? e.message : "Não foi possível exportar");
    } finally {
      setBusy(null);
    }
  }

  async function retrySend() {
    setBusy("send");
    setError(null);
    try {
      const out = await api<{ enviado: boolean; fhir_ref?: string; motivo?: string; consulta: Consultation }>(`/consultations/${c.id}/send/retry`, { method: "POST" });
      onChange(out.consulta);
      if (out.enviado) setMessage(`Enviado ao prontuário (${out.fhir_ref}).`);
      else setError(out.motivo ?? "Prontuário indisponível: tente de novo em instantes.");
    } catch (e) {
      setError(apiMessage(e, "Erro ao reenviar"));
    } finally {
      setBusy(null);
    }
  }

  async function downloadPdf() {
    setBusy("pdf");
    setError(null);
    try {
      await downloadFile(`/consultations/${c.id}/export.pdf`, `nota-consulta-${c.id}.pdf`);
    } catch (e) {
      const block = blockedBy(e);
      if (block) setBlocked(block);
      else setError(e instanceof Error ? e.message : "Não foi possível gerar o PDF");
    } finally {
      setBusy(null);
    }
  }

  async function send(withJustification = false) {
    if (!(await save(true))) return;
    setBusy("send");
    setError(null);
    try {
      const out = await api<{ enviado: boolean; status_envio?: string; fhir_ref?: string; motivo?: string; consulta: Consultation }>(
        `/consultations/${c.id}/send`,
        { method: "POST", json: withJustification ? { justificativa: justification.trim() } : undefined },
      );
      onChange(out.consulta);
      setBlocked(null);
      if (out.status_envio === "erro") setError(out.motivo ?? "Prontuário indisponível: a nota ficou na fila.");
      else setMessage(out.enviado ? `Enviado ao prontuário (${out.fhir_ref}).` : (out.motivo ?? "Exportado."));
    } catch (e) {
      const block = blockedBy(e);
      if (block) setBlocked(block);
      else setError(apiMessage(e, "Erro ao enviar"));
    } finally {
      setBusy(null);
    }
  }

  const minJustification = blocked ? MIN_JUSTIFICATION[blocked.codigo] : 10;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <FileCheck2 aria-hidden className="size-5 text-primary" />
          {surgical ? "Relatório cirúrgico para revisão" : "Nota da consulta para revisão"}
          {sent ? (
            <Badge variant="ok">
              <CircleCheck aria-hidden /> No prontuário
            </Badge>
          ) : (
            <AiBadge label="Rascunho gerado com IA — revise antes de enviar" />
          )}
        </CardTitle>
        <CardDescription>
          {c.nota_meta && (
            <>
              Gerada em {fmtDateTime(c.nota_meta.gerada_em)} · {c.nota_meta.modelo} · prompt v{c.nota_meta.prompt_version}
              {c.nota_meta.revisada_por && ` · revisada por ${c.nota_meta.revisada_por} em ${fmtDateTime(c.nota_meta.revisada_em!)}`}
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {c.nota_meta?.sem_ia && (
          <Alert variant="info" data-testid="nota-sem-ia">
            <ShieldAlert aria-hidden />
            <span>
              <Badge variant="outline">sem IA</Badge> O paciente não autorizou IA externa: esta nota foi montada só pelas regras do HousIA, a
              partir da transcrição.
            </span>
          </Alert>
        )}
        {c.nota_meta?.desatualizada && !sent && (
          <Alert variant="atencao" role="status" data-testid="nota-desatualizada">
            <ShieldAlert aria-hidden />
            <span>
              <b>Nota desatualizada:</b> a transcrição foi corrigida depois da nota (fala{c.nota_meta.desatualizada.falas.length > 1 ? "s" : ""}{" "}
              {c.nota_meta.desatualizada.falas.join(", ")}).{" "}
              {c.nota_meta.desatualizada.secoes.length > 0
                ? `Confira as seções que citam essas falas: ${c.nota_meta.desatualizada.secoes.join(", ")}.`
                : "Nenhuma seção cita essas falas, mas confira se o texto novo muda algo."}{" "}
              Salvar a revisão da nota confirma que ela está de acordo.
            </span>
          </Alert>
        )}
        {c.nota_meta?.aviso && (
          <Alert variant="atencao" data-testid="aviso-ia-indisponivel">
            <ShieldAlert aria-hidden />
            {c.nota_meta.aviso}
          </Alert>
        )}
        {sent && c.fhir_ref && (
          <Alert variant="ok">
            <CircleCheck aria-hidden />
            Enviado ao prontuário como <span className="font-mono">{c.fhir_ref}</span> em {fmtDateTime(c.sent_at!)} — já aparece na timeline
            do paciente.
          </Alert>
        )}

        {!surgical && c.nota && "copiloto" in c.nota && (c.nota.copiloto?.length ?? 0) > 0 && (
          <Alert variant="info">
            Inserido pelo copiloto (com as fontes):{" "}
            {c.nota.copiloto!.map((i) => i.destino === "pendencia" ? "pendência" : i.destino.replace("_", " ")).join(", ")}. Revise o
            texto antes de enviar.
          </Alert>
        )}

        {surgical && report ? (
          <div className="space-y-3">
            {(report.pedidos?.length ?? 0) > 0 && (
              <div className="rounded-md border bg-secondary/40 p-3">
                <p className="text-sm font-bold">Pedidos propostos (vão ao prontuário com o relatório aprovado)</p>
                <ul className="mt-1 space-y-1 text-sm">
                  {report.pedidos!.map((pedido, i) => (
                    <li key={pedido.descricao} className="flex flex-wrap items-center gap-2">
                      <span className="flex-1">
                        {pedido.descricao} <SourceRefs patientId={patient.id} refs={pedido.refs} />
                      </span>
                      {!sent && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          aria-label={`Retirar o pedido: ${pedido.descricao}`}
                          onClick={() =>
                            patchReport((rep) => {
                              rep.pedidos = (rep.pedidos ?? []).filter((_, j) => j !== i);
                            })
                          }
                        >
                          <Trash2 aria-hidden /> Retirar
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {report.prontidao && <ReadinessCard readiness={report.prontidao} patientId={patient.id} />}
            {report.opme.length > 0 && (
              <fieldset className="rounded-md border p-3" disabled={sent}>
                <legend className="px-1 text-sm font-bold">OPME e materiais (editável: o servidor mantém guia e etiquetas)</legend>
                <ul className="space-y-2">
                  {report.opme.map((o) => (
                    <li key={o.codigo} className="grid gap-2 text-sm sm:grid-cols-[1fr_6rem_1fr_auto] sm:items-end">
                      <span>
                        <span className="font-bold">{o.descricao}</span>{" "}
                        <Badge variant={OPME_STATUS[o.status].variant} className="text-[11px]">
                          {OPME_STATUS[o.status].label}
                        </Badge>
                        <span className="block text-xs text-muted-foreground">
                          Autorizado {o.autorizado} · lido {o.consumido}
                          {o.lotes.length > 0 && ` · lote ${o.lotes.map((l) => l.lote ?? "—").join(", ")}`}
                        </span>
                      </span>
                      <span className="space-y-1">
                        <Label htmlFor={`opme-qtd-${o.codigo}`} className="text-xs">
                          Citado
                        </Label>
                        <Input
                          id={`opme-qtd-${o.codigo}`}
                          type="number"
                          min={0}
                          max={99}
                          value={o.citado}
                          onChange={(e) => setOpme(o.codigo, { citado: Math.max(0, Math.min(99, Number(e.target.value) || 0)) })}
                        />
                      </span>
                      <span className="space-y-1">
                        <Label htmlFor={`opme-just-${o.codigo}`} className="text-xs">
                          Justificativa da divergência
                        </Label>
                        <Input
                          id={`opme-just-${o.codigo}`}
                          value={o.justificativa ?? ""}
                          maxLength={500}
                          onChange={(e) => setOpme(o.codigo, { justificativa: e.target.value })}
                        />
                      </span>
                      <Button type="button" variant="ghost" size="sm" aria-label={`Retirar ${o.descricao} do relatório`} onClick={() => setOpme(o.codigo, null)}>
                        <Trash2 aria-hidden />
                      </Button>
                    </li>
                  ))}
                </ul>
              </fieldset>
            )}
            {report.campos.map((f, i) => (
              <div key={f.campo} className="space-y-1">
                <Label htmlFor={`campo-${f.campo}`}>
                  {f.rotulo}
                  {f.obrigatorio && <span className="text-critico"> *</span>}
                </Label>
                <Textarea
                  id={`campo-${f.campo}`}
                  value={f.texto}
                  readOnly={sent}
                  onChange={(e) => setField(i, e.target.value)}
                  className="min-h-16"
                  placeholder={f.obrigatorio ? "Obrigatório — não foi ditado" : "Não ditado"}
                />
                {f.texto.trim() !== "" && f.refs.length === 0 && (
                  <Badge variant="atencao" className="text-[11px]">
                    sem fonte
                  </Badge>
                )}
                <SourceRefs patientId={patient.id} refs={f.refs} />
              </div>
            ))}
            <CidEditor cids={report.cids} disabled={sent} reid={r} onChange={(cids) => patchReport((rep) => { rep.cids = cids; })} />
            <fieldset className="space-y-2 rounded-md border p-3" disabled={sent}>
              <legend className="px-1 text-sm font-bold">Pendências do relatório (editáveis)</legend>
              {report.pendencias.length === 0 && <p className="text-sm text-muted-foreground">Nenhuma pendência.</p>}
              <ul className="space-y-2">
                {report.pendencias.map((p, i) => (
                  <li key={i} className="flex flex-wrap items-start gap-2 text-sm">
                    <Badge variant={p.severidade === "critico" ? "critico" : p.severidade === "atencao" ? "atencao" : "info"}>{p.severidade}</Badge>
                    <Textarea
                      aria-label={`Pendência ${i + 1}`}
                      value={p.texto}
                      className="min-h-11 flex-1"
                      onChange={(e) => patchReport((rep) => { rep.pendencias[i].texto = e.target.value; })}
                    />
                    <Button type="button" variant="ghost" size="sm" aria-label={`Retirar a pendência ${i + 1}`} onClick={() => patchReport((rep) => { rep.pendencias = rep.pendencias.filter((_, j) => j !== i); })}>
                      <Trash2 aria-hidden />
                    </Button>
                  </li>
                ))}
              </ul>
              <Button type="button" variant="outline" size="sm" onClick={() => void recompute()} disabled={busy !== null}>
                {busy === "recompute" ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />} Recalcular pendências e prontidão
              </Button>
            </fieldset>
          </div>
        ) : (
          <div className="space-y-3">
            {note.modelo && !sent && templates.length > 0 && (
              <div className="flex flex-wrap items-end gap-2 rounded-md border bg-secondary/40 p-3">
                <div className="space-y-1">
                  <Label htmlFor="modelo-nota">Modelo da nota</Label>
                  <NativeSelect id="modelo-nota" value={note.modelo.id} onChange={(e) => void switchTemplate(e.target.value)} className="h-9 w-72">
                    {templates.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.rotulo}
                        {t.sugerido ? " (sugerido)" : ""}
                      </option>
                    ))}
                  </NativeSelect>
                </div>
                <p className="text-xs text-muted-foreground">
                  v{note.modelo.versao} · documento LOINC {note.modelo.loinc.codigo} ({note.modelo.loinc.display}) · <span className="text-critico">*</span>{" "}
                  seção obrigatória: sem ela a nota não é aprovada
                </p>
              </div>
            )}
            {sections.map(({ key, rotulo, soap, obrigatoria, dica }) => {
              const section = readSection(note, key);
              return (
                <div key={key} className="space-y-1" data-testid={`secao-${key}`}>
                  <Label htmlFor={`sec-${key}`} className="flex items-center gap-2">
                    <span className="rounded bg-secondary px-1.5 font-mono text-xs">{soap}</span>
                    {rotulo}
                    {obrigatoria && <span className="text-critico" aria-hidden> *</span>}
                    {obrigatoria && <span className="sr-only">(obrigatória)</span>}
                    {semFonte(section) && (
                      <Badge variant="atencao" className="text-[11px]" data-testid={`sem-fonte-${key}`}>
                        sem fonte
                      </Badge>
                    )}
                  </Label>
                  <Textarea
                    id={`sec-${key}`}
                    value={r(section.texto)}
                    readOnly={sent}
                    required={obrigatoria}
                    aria-describedby={dica ? `dica-${key}` : undefined}
                    onChange={(e) => setSection(key, e.target.value)}
                    className="min-h-16"
                  />
                  {dica && (
                    <p id={`dica-${key}`} className="text-xs text-muted-foreground">
                      {dica}
                    </p>
                  )}
                  <SourceRefs patientId={patient.id} refs={section.refs} />
                  <SentenceSources section={section} patientId={patient.id} onPick={pickSource} />
                </div>
              );
            })}

            <CidEditor cids={note.cids} disabled={sent} reid={r} onChange={(cids) => setDraft((d) => ({ ...d, cids }))} />

            {(note.condicoes_propostas ?? []).length > 0 && (
              <div>
                <p className="text-sm font-bold">Diagnósticos propostos para o prontuário</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
                  {(note.condicoes_propostas ?? []).map((c) => (
                    <li key={c.codigo}>
                      <span className="font-mono">{c.codigo}</span> {r(c.descricao)} (
                      {c.verificacao === "confirmed" ? "confirmado" : "provisório"})
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {c.cobertura && <OutsideNote coverage={c.cobertura} disabled={sent} onInclude={include} targets={includeTargets} />}
            <DiscardedItems items={c.nota_meta?.descartados ?? []} />

            {note.pendencias.length > 0 && (
              <div>
                <p className="text-sm font-bold">Pendências</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
                  {note.pendencias.map((p) => (
                    <li key={p.texto}>
                      {r(p.texto)} <SourceRefs patientId={patient.id} refs={p.refs} />
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <details className="text-sm" open data-testid="cronologia">
              <summary className="min-h-9 cursor-pointer font-bold">Resumo cronológico da consulta ({note.cronologia.length})</summary>
              <ol className="mt-1 space-y-1">
                {note.cronologia.map((m, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-muted-foreground">{m.quando}</span>
                    {m.tipo && <Badge variant="outline" className="text-[10px]">{m.tipo}</Badge>}
                    <Input
                      aria-label={`Evento ${i + 1} da cronologia`}
                      value={r(m.texto)}
                      readOnly={sent}
                      className="min-h-9 min-w-48 flex-1"
                      onChange={(e) => setList("cronologia", note.cronologia.map((x, j) => (j === i ? { ...x, texto: e.target.value } : x)))}
                    />
                    <SourceRefs patientId={patient.id} refs={m.refs} />
                    {!sent && (
                      <Button type="button" variant="ghost" size="sm" aria-label={`Remover o evento ${i + 1} da cronologia`} onClick={() => setList("cronologia", note.cronologia.filter((_, j) => j !== i))}>
                        <Trash2 aria-hidden />
                      </Button>
                    )}
                  </li>
                ))}
              </ol>
              {!sent && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-1"
                  onClick={() => setList("cronologia", [...note.cronologia, { quando: "", texto: "", refs: [] } as ChronoItem])}
                >
                  <Plus aria-hidden /> Adicionar evento
                </Button>
              )}
            </details>

            <div className="rounded-md border bg-secondary/40 p-3">
              <p className="text-sm font-bold">Orientações ao paciente (linguagem simples, editáveis)</p>
              <ul className="mt-1 space-y-1.5">
                {note.orientacoes_paciente.map((o, i) => (
                  <li key={i} className="flex items-start gap-2">
                    <Textarea
                      aria-label={`Orientação ${i + 1}`}
                      value={r(o)}
                      readOnly={sent}
                      className="min-h-11 flex-1 bg-card"
                      onChange={(e) => setList("orientacoes_paciente", note.orientacoes_paciente.map((x, j) => (j === i ? e.target.value : x)))}
                    />
                    {!sent && (
                      <Button type="button" variant="ghost" size="sm" aria-label={`Remover a orientação ${i + 1}`} onClick={() => setList("orientacoes_paciente", note.orientacoes_paciente.filter((_, j) => j !== i))}>
                        <Trash2 aria-hidden />
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              {!sent && (
                <Button type="button" variant="outline" size="sm" className="mt-1" onClick={() => setList("orientacoes_paciente", [...note.orientacoes_paciente, ""])}>
                  <Plus aria-hidden /> Adicionar orientação
                </Button>
              )}
            </div>

            {(c.insights?.pedidos?.length ?? 0) > 0 && (
              <section aria-labelledby="pedidos-rascunho" className="rounded-md border p-3">
                <h3 id="pedidos-rascunho" className="mb-2 text-sm font-bold">
                  Pedidos e prescrições em rascunho
                </h3>
                <OrdersCard
                  consultId={c.id}
                  orders={c.insights?.pedidos ?? []}
                  editable={!sent}
                  onInsights={(insights) => onChange({ ...c, insights })}
                />
              </section>
            )}
          </div>
        )}

        {!surgical && (c.propostas?.length ?? 0) > 0 && (
          <ProposalsPanel
            consultId={c.id}
            patientId={patient.id}
            proposals={c.propostas ?? []}
            editable={!sent}
            onChange={(propostas) => onChange({ ...c, propostas })}
          />
        )}

        {!surgical && c.nota && <DocumentsPanel consultId={c.id} patientId={patient.id} ready />}

        {c.envio?.status === "erro" && !sent && (
          <Alert variant="atencao" data-testid="envio-erro">
            <ShieldAlert aria-hidden />
            <span className="flex-1">
              <b>Envio ao prontuário falhou</b> (tentativa {c.envio.tentativas}): a nota ficou na fila e o reenvio não duplica nada.
              {c.envio.ultimo_erro && <span className="block text-xs">{c.envio.ultimo_erro}</span>}
            </span>
            <Button variant="outline" onClick={() => void retrySend()} disabled={busy !== null}>
              {busy === "send" ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />} Reenviar
            </Button>
          </Alert>
        )}

        {diff && <DiffPanel diff={diff} />}

        {error && <Alert variant="critico">{error}</Alert>}
        {blocked && (
          <Alert variant="critico" className="flex-col" data-testid="trava-envio">
            <p className="font-bold">{blocked.mensagem}</p>
            <ul className="list-disc space-y-0.5 pl-5">
              {blocked.pendencias.map((p, i) => (
                <li key={i}>
                  {p.texto} {p.fonte && <span className="text-xs">(Fonte: {p.fonte})</span>}
                </li>
              ))}
            </ul>
            <Label htmlFor="justificativa-envio">Justificativa do envio com pendência crítica (fica no registro de auditoria)</Label>
            <Textarea id="justificativa-envio" value={justification} onChange={(e) => setJustification(e.target.value)} className="min-h-16 bg-card text-foreground" />
            <Button onClick={() => void send(true)} disabled={busy !== null || justification.trim().length < minJustification} className="self-start">
              <Send aria-hidden /> Enviar com justificativa
            </Button>
          </Alert>
        )}
        {message && (
          <p role="status" className="text-sm font-bold text-ok">
            {message}
          </p>
        )}

        {!sent && (
          <div className="flex items-start gap-2 rounded-md border bg-secondary/40 p-3">
            <Checkbox id="atestacao" checked={attested} onCheckedChange={(v) => setAttested(v === true)} className="mt-0.5" />
            <Label htmlFor="atestacao" className="cursor-pointer leading-snug">
              Revisei e assumo a responsabilidade por esta nota
              <span className="block text-xs font-normal text-muted-foreground">
                O servidor só exporta ou envia a nota revisada com esta atestação; ao editar de novo, atestar outra vez.
              </span>
            </Label>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {!sent && (
            <Button variant="outline" onClick={() => void save()} disabled={busy !== null}>
              {busy === "save" ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />} Salvar revisão
            </Button>
          )}
          <Button variant="outline" onClick={() => void loadDiff()} disabled={busy !== null || !c.nota_meta?.revisada_por}>
            <GitCompareArrows aria-hidden /> Comparar com o rascunho da IA
          </Button>
          <Button variant="outline" onClick={() => void copy()} disabled={busy !== null || !approved || dirty}>
            <ClipboardCopy aria-hidden /> Copiar para o prontuário
          </Button>
          <Button variant="outline" onClick={() => void download()} disabled={busy !== null || !approved || dirty}>
            <Download aria-hidden /> Baixar FHIR
          </Button>
          <Button variant="outline" onClick={() => void downloadPdf()} disabled={busy !== null || !approved || dirty}>
            {busy === "pdf" ? <Loader2 aria-hidden className="animate-spin" /> : <FileText aria-hidden />} Baixar PDF
          </Button>
          {!sent && (
            <Button onClick={() => void send()} disabled={busy !== null || !attested}>
              {busy === "send" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
              {canSend ? "Aprovar e enviar ao prontuário" : "Aprovar e exportar"}
            </Button>
          )}
        </div>
        {!sent && (!approved || dirty) && (
          <p className="text-xs text-muted-foreground">
            Copiar e baixar usam a nota já aprovada no servidor: salve a revisão com a atestação (ou use “Aprovar e enviar”) antes.
          </p>
        )}
        <FeedbackButtons kind="consulta_nota" entityId={c.id} patientId={patient.id} />
        {sent && <NpsPrompt />}
      </CardContent>
    </Card>
  );
}
