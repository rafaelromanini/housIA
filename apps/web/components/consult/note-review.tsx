"use client";

import { useState } from "react";
import { CircleCheck, ClipboardCopy, Download, FileCheck2, Loader2, Send, Trash2 } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { usePatient } from "@/components/patient/patient-context";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { OPME_STATUS } from "@/components/consult/opme-panel";
import { ReadinessCard } from "@/components/consult/readiness-card";
import { OrdersCard } from "@/components/consult/orders-card";
import { api, ApiError, type BillingItem, type ConsultNote, type Consultation, type OpmeLine, type SurgicalReport } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";

const SECTIONS: { key: keyof ConsultNote; label: string; soap: string }[] = [
  { key: "queixa_principal", label: "Queixa principal", soap: "S" },
  { key: "hda", label: "História da doença atual", soap: "S" },
  { key: "antecedentes", label: "Antecedentes", soap: "S" },
  { key: "medicacoes", label: "Medicações em uso", soap: "S" },
  { key: "alergias", label: "Alergias", soap: "S" },
  { key: "exame_fisico", label: "Exame físico", soap: "O" },
  { key: "exames_complementares", label: "Exames complementares", soap: "O" },
  { key: "avaliacao", label: "Avaliação", soap: "A" },
  { key: "plano", label: "Plano", soap: "P" },
];

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

  function setSection(key: keyof ConsultNote, texto: string) {
    setDraft((d) => ({ ...d, [key]: { ...(d[key] as object), texto } }));
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
      setError(e instanceof Error ? e.message : "Erro ao salvar");
      return null;
    } finally {
      setBusy(null);
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
      else setError(e instanceof Error ? e.message : "Erro ao enviar");
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
        {sent && c.fhir_ref && (
          <Alert variant="ok">
            <CircleCheck aria-hidden />
            Enviado ao prontuário como <span className="font-mono">{c.fhir_ref}</span> em {fmtDateTime(c.sent_at!)} — já aparece na timeline
            do paciente.
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
            {SECTIONS.map(({ key, label, soap }) => {
              const section = note[key] as { texto: string; refs: string[] };
              return (
                <div key={key} className="space-y-1">
                  <Label htmlFor={`sec-${key}`} className="flex items-center gap-2">
                    <span className="rounded bg-secondary px-1.5 font-mono text-xs">{soap}</span>
                    {label}
                  </Label>
                  <Textarea
                    id={`sec-${key}`}
                    value={r(section.texto)}
                    readOnly={sent}
                    onChange={(e) => setSection(key, e.target.value)}
                    className="min-h-16"
                  />
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
      </CardContent>
    </Card>
  );
}
