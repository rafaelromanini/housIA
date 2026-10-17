"use client";

import { useEffect, useState } from "react";
import { CircleCheck, ClipboardCopy, Download, FileCheck2, Loader2, Send, ShieldAlert, Trash2 } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { NpsPrompt } from "@/components/ai/nps-prompt";
import { usePatient } from "@/components/patient/patient-context";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { OPME_STATUS } from "@/components/consult/opme-panel";
import { ReadinessCard } from "@/components/consult/readiness-card";
import { OrdersCard } from "@/components/consult/orders-card";
import { api, ApiError, type BillingItem, type ConsultNote, type Consultation, type NoteCoverage, type NoteSectionDef, type NoteTemplateInfo, type OpmeLine, type SurgicalReport } from "@/lib/api";
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
function readSection(note: ConsultNote, key: string): { texto: string; refs: string[] } {
  if (FIXED_KEYS.includes(key)) return (note as unknown as Record<string, { texto: string; refs: string[] }>)[key] ?? { texto: "", refs: [] };
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

type Blocked = { mensagem: string; pendencias: BillingItem[] };

/** 409 da trava de envio: pendência crítica de faturamento sem justificativa. */
function blockedBy(e: unknown): Blocked | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = (e.body as { detail?: { codigo?: string; mensagem?: string; pendencias?: BillingItem[] } } | null)?.detail;
  return detail?.codigo === "prontidao_critica" ? { mensagem: detail.mensagem ?? "", pendencias: detail.pendencias ?? [] } : null;
}

/** Revisão obrigatória: o profissional edita, aprova e só então a nota vai para o prontuário. */
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
  const [busy, setBusy] = useState<"save" | "send" | "copy" | "download" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sent = c.status === "enviada";
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [justification, setJustification] = useState("");

  const note = draft as unknown as ConsultNote;
  const report = (draft as { relatorio_cirurgico?: SurgicalReport }).relatorio_cirurgico;

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
  function setOpme(code: string, patch: Partial<OpmeLine> | null) {
    setDraft((d) => {
      const rep = structuredClone((d as { relatorio_cirurgico: SurgicalReport }).relatorio_cirurgico);
      rep.opme = patch === null ? rep.opme.filter((o) => o.codigo !== code) : rep.opme.map((o) => (o.codigo === code ? { ...o, ...patch } : o));
      return { ...d, relatorio_cirurgico: rep };
    });
  }
  function setField(i: number, texto: string) {
    setDraft((d) => {
      const rep = structuredClone((d as { relatorio_cirurgico: SurgicalReport }).relatorio_cirurgico);
      rep.campos[i].texto = texto;
      return { ...d, relatorio_cirurgico: rep };
    });
  }

  async function save(): Promise<Consultation | null> {
    setBusy("save");
    setError(null);
    try {
      const updated = await api<Consultation>(`/consultations/${c.id}/note`, { method: "PUT", json: draft });
      onChange(updated);
      // o servidor recalcula a conciliação de OPME, as pendências e a prontidão: o rascunho mostra o que ele devolveu
      const saved = updated.nota && "relatorio_cirurgico" in updated.nota ? updated.nota.relatorio_cirurgico : undefined;
      if (saved) {
        setDraft((d) => {
          const rep = (d as { relatorio_cirurgico?: SurgicalReport }).relatorio_cirurgico;
          return rep ? { ...d, relatorio_cirurgico: { ...rep, opme: saved.opme, pendencias: saved.pendencias, prontidao: saved.prontidao } } : d;
        });
      }
      setMessage("Revisão salva.");
      return updated;
    } catch (e) {
      setError(apiMessage(e, "Erro ao salvar"));
      return null;
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
      onChange({ ...c, nota: out.nota as unknown as Consultation["nota"], cobertura: out.cobertura });
      setMessage(`Fala ${n} incluída na nota.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível incluir a fala");
    }
  }

  async function exportOut(): Promise<ExportOut> {
    return api<ExportOut>(`/consultations/${c.id}/export`);
  }

  async function copy() {
    setBusy("copy");
    try {
      if (!sent && !(await save())) return;
      const out = await exportOut();
      await navigator.clipboard.writeText(out.texto);
      setMessage("Texto copiado: cole no prontuário.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível copiar");
    } finally {
      setBusy(null);
    }
  }

  async function download() {
    setBusy("download");
    try {
      if (!sent && !(await save())) return;
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
      setError(e instanceof Error ? e.message : "Não foi possível exportar");
    } finally {
      setBusy(null);
    }
  }

  async function send(withJustification = false) {
    if (!(await save())) return;
    setBusy("send");
    setError(null);
    try {
      const out = await api<{ enviado: boolean; fhir_ref?: string; motivo?: string; consulta: Consultation }>(
        `/consultations/${c.id}/send`,
        { method: "POST", json: withJustification ? { justificativa: justification.trim() } : undefined },
      );
      onChange(out.consulta);
      setBlocked(null);
      setMessage(out.enviado ? `Enviado ao prontuário (${out.fhir_ref}).` : (out.motivo ?? "Exportado."));
    } catch (e) {
      const block = blockedBy(e);
      if (block) setBlocked(block);
      else setError(apiMessage(e, "Erro ao enviar"));
    } finally {
      setBusy(null);
    }
  }

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
                            setDraft((d) => {
                              const rep = structuredClone((d as { relatorio_cirurgico: SurgicalReport }).relatorio_cirurgico);
                              rep.pedidos = (rep.pedidos ?? []).filter((_, j) => j !== i);
                              return { ...d, relatorio_cirurgico: rep };
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
            {c.nota && "relatorio_cirurgico" in c.nota && c.nota.relatorio_cirurgico?.prontidao && (
              <ReadinessCard readiness={c.nota.relatorio_cirurgico.prontidao} patientId={patient.id} />
            )}
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
                <SourceRefs patientId={patient.id} refs={f.refs} />
              </div>
            ))}
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
                </div>
              );
            })}

            <div>
              <p className="text-sm font-bold">CID-10 sugeridos</p>
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {note.cids.map((cid, i) => (
                  <li key={cid.codigo + i}>
                    <Badge variant={cid.tipo === "hipotese" ? "ai" : "outline"} className="gap-1.5">
                      <span className="font-mono">{cid.codigo}</span> {r(cid.descricao)}
                      <span className="text-[10px] uppercase">{cid.tipo}</span>
                      {!sent && (
                        <button
                          type="button"
                          aria-label={`Remover CID ${cid.codigo}`}
                          className="ml-0.5 rounded p-0.5 hover:bg-muted"
                          onClick={() => setDraft((d) => ({ ...d, cids: note.cids.filter((_, j) => j !== i) }))}
                        >
                          <Trash2 aria-hidden className="size-3.5" />
                        </button>
                      )}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>

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

            {note.cronologia.length > 0 && (
              <details className="text-sm">
                <summary className="min-h-9 cursor-pointer font-bold">Linha do tempo da consulta ({note.cronologia.length})</summary>
                <ol className="mt-1 space-y-0.5">
                  {note.cronologia.map((m, i) => (
                    <li key={i} className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-muted-foreground">{m.quando}</span> {r(m.texto)}
                      <SourceRefs patientId={patient.id} refs={m.refs} />
                    </li>
                  ))}
                </ol>
              </details>
            )}

            <div className="rounded-md border bg-secondary/40 p-3">
              <p className="text-sm font-bold">Orientações ao paciente (linguagem simples)</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
                {note.orientacoes_paciente.map((o) => (
                  <li key={o}>{r(o)}</li>
                ))}
              </ul>
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

        {error && <Alert variant="critico">{error}</Alert>}
        {blocked && (
          <Alert variant="critico" className="flex-col">
            <p className="font-bold">{blocked.mensagem}</p>
            <ul className="list-disc space-y-0.5 pl-5">
              {blocked.pendencias.map((p) => (
                <li key={p.regra + (p.campo ?? "")}>
                  {p.texto} <span className="text-xs">(Fonte: {p.fonte})</span>
                </li>
              ))}
            </ul>
            <Label htmlFor="justificativa-envio">Justificativa do envio com pendência crítica (fica no registro de auditoria)</Label>
            <Textarea id="justificativa-envio" value={justification} onChange={(e) => setJustification(e.target.value)} className="min-h-16 bg-card text-foreground" />
            <Button onClick={() => void send(true)} disabled={busy !== null || justification.trim().length < 15} className="self-start">
              <Send aria-hidden /> Enviar com justificativa
            </Button>
          </Alert>
        )}
        {message && (
          <p role="status" className="text-sm font-bold text-ok">
            {message}
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          {!sent && (
            <Button variant="outline" onClick={() => void save()} disabled={busy !== null}>
              {busy === "save" ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />} Salvar revisão
            </Button>
          )}
          <Button variant="outline" onClick={() => void copy()} disabled={busy !== null}>
            <ClipboardCopy aria-hidden /> Copiar para o prontuário
          </Button>
          <Button variant="outline" onClick={() => void download()} disabled={busy !== null}>
            <Download aria-hidden /> Baixar FHIR
          </Button>
          {!sent && (
            <Button onClick={() => void send()} disabled={busy !== null}>
              {busy === "send" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
              {canSend ? "Aprovar e enviar ao prontuário" : "Aprovar e exportar"}
            </Button>
          )}
        </div>
        <FeedbackButtons kind="consulta_nota" entityId={c.id} patientId={patient.id} />
        {sent && <NpsPrompt />}
      </CardContent>
    </Card>
  );
}
