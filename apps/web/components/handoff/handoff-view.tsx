"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowRightLeft,
  Clock,
  ClipboardCopy,
  CircleHelp,
  ListChecks,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Stethoscope,
  HeartPulse,
} from "lucide-react";

import { AckButton } from "@/components/ai/alert-ack";
import { FeedbackButtons } from "@/components/ai/feedback";
import { VerbalCheck } from "@/components/handoff/verbal-check";
import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityBadge, SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type AlertItem, type Audience, type HandoffReport } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const AUDIENCES: { value: Audience; label: string; Icon: typeof Stethoscope }[] = [
  { value: "medico", label: "Médico(a)", Icon: Stethoscope },
  { value: "enfermagem", label: "Enfermagem", Icon: HeartPulse },
];

export function HandoffView() {
  const { patient, reload: reloadPatient } = usePatient();
  const { user } = useAuth();
  const [audience, setAudience] = useState<Audience>(user?.role === "enfermagem" ? "enfermagem" : "medico");
  const [windowH, setWindowH] = useState(12);
  const [destino, setDestino] = useState("");
  const [report, setReport] = useState<HandoffReport | null>(null);
  const [alerts, setAlerts] = useState<Record<number, AlertItem>>({});
  const [loadingLatest, setLoadingLatest] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadAlerts = useCallback(async () => {
    const list = await api<AlertItem[]>(`/patients/${patient.id}/alerts`);
    setAlerts(Object.fromEntries(list.map((a) => [a.id, a])));
  }, [patient.id]);

  useEffect(() => {
    let alive = true;
    setLoadingLatest(true);
    setError(null);
    api<HandoffReport | null>(`/patients/${patient.id}/handoff/latest?audience=${audience}`)
      .then((r) => alive && setReport(r))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoadingLatest(false));
    void loadAlerts();
    return () => {
      alive = false;
    };
  }, [patient.id, audience, loadAlerts]);

  async function generate(force: boolean) {
    setGenerating(true);
    setError(null);
    try {
      const r = await api<HandoffReport>(`/patients/${patient.id}/handoff`, {
        method: "POST",
        json: { audience, window_hours: windowH, to_location: destino.trim() || null, force },
      });
      setReport(r);
      await loadAlerts();
      void reloadPatient();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao gerar a passagem");
    } finally {
      setGenerating(false);
    }
  }

  const c = report?.content;
  const name = patient.nome;
  const plainText = useMemo(() => (c ? toPlainText(c, name, patient) : ""), [c, name, patient]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(plainText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Não foi possível copiar para a área de transferência.");
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-4">
          <form
            className="grid gap-3 md:grid-cols-[auto_minmax(0,160px)_minmax(0,1fr)_auto] md:items-end"
            onSubmit={(e) => {
              e.preventDefault();
              void generate(false);
            }}
          >
            <fieldset className="space-y-1.5">
              <legend className="text-sm font-bold">Público</legend>
              <div className="inline-flex rounded-md border bg-muted p-1" role="radiogroup" aria-label="Público">
                {AUDIENCES.map(({ value, label, Icon }) => (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={audience === value}
                    onClick={() => setAudience(value)}
                    className={cn(
                      "inline-flex min-h-10 items-center gap-2 rounded px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      audience === value ? "bg-card text-primary shadow-sm" : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <Icon aria-hidden className="size-4" />
                    {label}
                  </button>
                ))}
              </div>
            </fieldset>
            <div className="space-y-1.5">
              <Label htmlFor="janela">Janela</Label>
              <NativeSelect id="janela" value={windowH} onChange={(e) => setWindowH(Number(e.target.value))}>
                <option value={6}>Últimas 6h</option>
                <option value={12}>Últimas 12h</option>
                <option value={24}>Últimas 24h</option>
              </NativeSelect>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="destino">Destino (opcional)</Label>
              <Input id="destino" placeholder="Ex.: próximo turno, Enfermaria 3B" value={destino} onChange={(e) => setDestino(e.target.value)} />
            </div>
            <Button type="submit" disabled={generating}>
              {generating ? <Loader2 aria-hidden className="animate-spin" /> : <Sparkles aria-hidden />}
              {generating ? "Gerando…" : "Gerar passagem"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <VerbalCheck audience={audience} windowH={windowH} />

      {error && (
        <Alert variant="critico">
          <ShieldAlert aria-hidden />
          <div>
            <p className="font-bold">Não foi possível gerar a passagem</p>
            <p>{error}</p>
          </div>
        </Alert>
      )}

      {(generating || loadingLatest) && (
        <div className="space-y-3" aria-live="polite">
          {generating && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 aria-hidden className="size-4 animate-spin" /> Lendo o prontuário e cruzando fatos (pode levar até 1 minuto)…
            </p>
          )}
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      )}

      {!generating && !loadingLatest && !c && !error && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <ListChecks aria-hidden className="size-10 text-muted-foreground" />
            <p className="font-bold">Nenhuma passagem gerada para este público ainda.</p>
            <p className="max-w-md text-sm text-muted-foreground">
              O HousIA lê a timeline da janela escolhida, calcula fatos objetivos (sinais vitais, diurese, horários) e gera um
              resumo SBAR com a fonte de cada afirmação.
            </p>
          </CardContent>
        </Card>
      )}

      {!generating && report && c && (
        <article className="space-y-4" aria-label="Passagem de plantão">
          <div className="flex flex-wrap items-center gap-2">
            <AiBadge />
            {c.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
            {report.cached && <Badge variant="secondary">Do cache</Badge>}
            <span className="text-xs text-muted-foreground">
              Gerado em {fmtDateTime(report.created_at)} · janela {report.window_hours}h · {report.model} · prompt v
              {report.prompt_version}
            </span>
            <div className="ml-auto flex gap-2">
              <Button variant="outline" size="sm" onClick={() => void copy()}>
                <ClipboardCopy aria-hidden />
                {copied ? "Copiado!" : "Copiar"}
              </Button>
              <Button variant="outline" size="sm" onClick={() => void generate(true)}>
                <RefreshCw aria-hidden />
                Regenerar
              </Button>
            </div>
          </div>

          <div className="rounded-lg border border-l-4 border-l-primary bg-card p-4 shadow-sm">
            <p className="text-xs font-bold uppercase tracking-wide text-primary">Em uma frase</p>
            <p className="mt-1 text-lg font-bold leading-snug">{reidentify(c.headline, name)}</p>
          </div>

          {c.descartados.length > 0 && (
            <Alert variant="info">
              <ShieldAlert aria-hidden />
              <details>
                <summary className="cursor-pointer font-bold">
                  {c.descartados.length} item(ns) gerado(s) pela IA foram descartados por não citarem uma fonte válida
                </summary>
                <ul className="mt-2 list-disc pl-4">
                  {c.descartados.map((d, i) => (
                    <li key={i}>
                      {d.secao}: {d.texto} ({d.motivo})
                    </li>
                  ))}
                </ul>
              </details>
            </Alert>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>Situação</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1">
                <p>{reidentify(c.situacao, name)}</p>
                <SourceRefs patientId={patient.id} refs={c.situacao_refs} />
              </CardContent>
            </Card>

            <Card className="lg:col-span-2 border-critico-border/60">
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <ShieldAlert aria-hidden className="size-5 text-critico" /> Riscos nas próximas horas
                </CardTitle>
              </CardHeader>
              <CardContent>
                {c.riscos_proximas_horas.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Nenhum risco com fonte válida identificado.</p>
                ) : (
                  <ul className="space-y-2">
                    {c.riscos_proximas_horas.map((r, i) => (
                      <li
                        key={i}
                        className={cn(
                          "flex gap-3 rounded-md border p-3",
                          r.severidade === "critico" && "border-critico-border bg-critico-bg/60",
                          r.severidade === "atencao" && "border-atencao-border bg-atencao-bg/60",
                          r.severidade === "info" && "border-info-border bg-info-bg/40",
                        )}
                      >
                        <SeverityIcon severity={r.severidade} className="mt-0.5 size-5 shrink-0" />
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <SeverityBadge severity={r.severidade} />
                            <span className="inline-flex items-center gap-1 text-xs font-bold text-muted-foreground">
                              <Clock aria-hidden className="size-3.5" /> em até {r.horizonte_h}h
                            </span>
                          </div>
                          <p>{reidentify(r.texto, name)}</p>
                          <div className="flex flex-wrap items-center gap-2">
                            <SourceRefs patientId={patient.id} refs={r.refs} />
                            {r.alert_id && (
                              <AckButton
                                alert={alerts[r.alert_id]}
                                onChange={(a) => {
                                  setAlerts((prev) => ({ ...prev, [a.id]: a }));
                                  void reloadPatient();
                                }}
                              />
                            )}
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <ArrowRightLeft aria-hidden className="size-4 text-primary" /> Mudanças no período
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ItemList items={c.mudancas_no_periodo} patientId={patient.id} name={name} empty="Sem mudanças com fonte válida." />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <ListChecks aria-hidden className="size-4 text-primary" /> Pendências
                </CardTitle>
              </CardHeader>
              <CardContent>
                {c.pendencias.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Sem pendências registradas.</p>
                ) : (
                  <ul className="divide-y">
                    {c.pendencias.map((p, i) => (
                      <li key={i} className="flex gap-3 py-2">
                        <span
                          className={cn(
                            "inline-flex h-7 min-w-14 items-center justify-center rounded border px-1.5 text-sm font-bold tabular-nums",
                            p.horario.includes(":") ? "border-primary text-primary" : "text-muted-foreground",
                          )}
                        >
                          {p.horario.includes(":") ? p.horario : "—"}
                        </span>
                        <div className="min-w-0">
                          <p>{reidentify(p.texto, name)}</p>
                          <SourceRefs patientId={patient.id} refs={p.refs} />
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <CircleHelp aria-hidden className="size-4 text-primary" /> Perguntas para quem está saindo
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ol className="list-decimal space-y-1 pl-5">
                  {c.perguntas_para_quem_sai.map((q, i) => (
                    <li key={i}>{reidentify(q, name)}</li>
                  ))}
                </ol>
              </CardContent>
            </Card>
          </div>

          <FeedbackButtons kind="handoff" entityId={report.id} patientId={patient.id} />

          <details className="rounded-lg border bg-card p-4">
            <summary className="cursor-pointer font-bold">
              Fatos determinísticos usados ({c.fatos.length}) — calculados do prontuário, sem IA
            </summary>
            <ul className="mt-3 space-y-2 text-sm">
              {c.fatos.map((f, i) => (
                <li key={i} className="flex flex-wrap items-start gap-2">
                  <SeverityBadge severity={f.severidade} />
                  <span className="min-w-0 flex-1">{f.texto}</span>
                  <SourceRefs patientId={patient.id} refs={f.refs} />
                </li>
              ))}
            </ul>
          </details>
        </article>
      )}
    </div>
  );
}

function ItemList({ items, patientId, name, empty }: { items: { texto: string; refs: string[] }[]; patientId: string; name: string; empty: string }) {
  if (items.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="divide-y">
      {items.map((it, i) => (
        <li key={i} className="py-2">
          <p>{reidentify(it.texto, name)}</p>
          <SourceRefs patientId={patientId} refs={it.refs} />
        </li>
      ))}
    </ul>
  );
}

function toPlainText(c: NonNullable<HandoffReport["content"]>, name: string, p: { setor: string | null; leito: string | null }) {
  const r = (t: string) => reidentify(t, name);
  const lines = [
    `PASSAGEM DE PLANTÃO — ${name} (${p.setor ?? ""}, leito ${p.leito ?? ""}) — público: ${c.meta.publico}`,
    "",
    r(c.headline),
    "",
    "SITUAÇÃO",
    r(c.situacao),
    "",
    "RISCOS NAS PRÓXIMAS HORAS",
    ...c.riscos_proximas_horas.map((x) => `- [${x.severidade.toUpperCase()}, até ${x.horizonte_h}h] ${r(x.texto)}`),
    "",
    "MUDANÇAS NO PERÍODO",
    ...c.mudancas_no_periodo.map((x) => `- ${r(x.texto)}`),
    "",
    "PENDÊNCIAS",
    ...c.pendencias.map((x) => `- ${x.horario}: ${r(x.texto)}`),
    "",
    "PERGUNTAS PARA QUEM SAI",
    ...c.perguntas_para_quem_sai.map((x) => `- ${r(x)}`),
    "",
    "Gerado por IA (HousIA) — conferir com o prontuário. Ferramenta de apoio à decisão; não substitui avaliação profissional.",
  ];
  return lines.join("\n");
}
