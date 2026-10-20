"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowRight, Bot, ClipboardPen, FlaskConical, Loader2, PlayCircle, ShieldAlert, TableProperties } from "lucide-react";

import { AckButton } from "@/components/ai/alert-ack";
import { reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityBadge, SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type AlertItem, type MedrecRun, type RecRow } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const STATUS: Record<RecRow["status"], { label: string; variant: BadgeProps["variant"] }> = {
  omitida: { label: "Omitida?", variant: "critico" },
  alterada: { label: "Alterada", variant: "atencao" },
  nova: { label: "Nova", variant: "info" },
  suspensa: { label: "Suspensa", variant: "secondary" },
  mantida: { label: "Mantida", variant: "outline" },
};

const FINDING_LABEL: Record<string, string> = {
  omissao: "Omissão",
  alergia: "Alergia",
  alergia_cruzada: "Reação cruzada",
  interacao: "Interação",
  duplicidade: "Duplicidade",
  alto_alerta: "Alto alerta",
};

const CONFIDENCE: Record<string, string> = { alta: "Confiança alta", media: "Confiança média", baixa: "Confiança baixa" };

export function MedrecView() {
  const { patient, reload: reloadPatient } = usePatient();
  const sectors = patient.setores;
  const current = sectors.length - 1;
  const [origin, setOrigin] = useState(sectors.length > 1 ? sectors[current - 1].encounter_id : "");
  const [dest, setDest] = useState(sectors[current]?.encounter_id ?? "");
  const [run, setRun] = useState<MedrecRun | null>(null);
  const [alerts, setAlerts] = useState<Record<number, AlertItem>>({});
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAlerts = useCallback(async () => {
    const list = await api<AlertItem[]>(`/patients/${patient.id}/alerts`);
    setAlerts(Object.fromEntries(list.map((a) => [a.id, a])));
  }, [patient.id]);

  useEffect(() => {
    let alive = true;
    api<MedrecRun | null>(`/patients/${patient.id}/medrec/latest`)
      .then((r) => alive && setRun(r))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    void loadAlerts();
    return () => {
      alive = false;
    };
  }, [patient.id, loadAlerts]);

  async function execute() {
    setRunning(true);
    setError(null);
    try {
      const r = await api<MedrecRun>(`/patients/${patient.id}/medrec`, {
        method: "POST",
        json: { from: origin || null, to: dest },
      });
      setRun(r);
      await loadAlerts();
      void reloadPatient();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro na reconciliação");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-4">
          <form
            className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto] md:items-end"
            onSubmit={(e) => {
              e.preventDefault();
              void execute();
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="origem">Prescrição de origem</Label>
              <NativeSelect id="origem" value={origin} onChange={(e) => setOrigin(e.target.value)}>
                <option value="">Sem origem (só checagens da prescrição atual)</option>
                {sectors.map((s) => (
                  <option key={s.encounter_id} value={s.encounter_id}>
                    {s.setor} · leito {s.leito}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <ArrowRight aria-hidden className="hidden size-5 self-center text-muted-foreground md:mt-6 md:block" />
            <div className="space-y-1.5">
              <Label htmlFor="destino-rec">Prescrição de destino</Label>
              <NativeSelect id="destino-rec" value={dest} onChange={(e) => setDest(e.target.value)}>
                {sectors.map((s) => (
                  <option key={s.encounter_id} value={s.encounter_id}>
                    {s.setor} · leito {s.leito}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <Button type="submit" disabled={running || !dest}>
              {running ? <Loader2 aria-hidden className="animate-spin" /> : <PlayCircle aria-hidden />}
              {running ? "Reconciliando…" : "Executar reconciliação"}
            </Button>
          </form>
        </CardContent>
      </Card>

      {error && (
        <Alert variant="critico">
          <ShieldAlert aria-hidden />
          {error}
        </Alert>
      )}

      {(loading || running) && (
        <div className="space-y-3" aria-live="polite">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      )}

      {!loading && !running && !run && !error && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
            <TableProperties aria-hidden className="size-10 text-muted-foreground" />
            <p className="font-bold">Nenhuma reconciliação executada para este paciente.</p>
            <p className="max-w-md text-sm text-muted-foreground">
              Compara a prescrição em uso na saída do setor de origem com a do destino: omissões, duplicidades, interações,
              medicamentos de alto alerta e alergias. Depois, a IA avalia se sintomas novos podem ser efeito adverso ou retirada.
            </p>
          </CardContent>
        </Card>
      )}

      {!running && run && (
        <div className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Executada em {fmtDateTime(run.created_at)} · {run.origem ? `${run.origem.setor} → ` : ""}
            {run.destino.setor}
          </p>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert aria-hidden className="size-5 text-critico" /> Alertas das regras ({run.achados.length})
              </CardTitle>
              <CardDescription>Camada determinística: sempre executada, não depende de IA.</CardDescription>
            </CardHeader>
            <CardContent>
              {run.achados.length === 0 ? (
                <p className="text-sm text-muted-foreground">Nenhum achado nas regras.</p>
              ) : (
                <ul className="space-y-2">
                  {run.achados.map((f, i) => (
                    <li
                      key={i}
                      className={cn(
                        "flex gap-3 rounded-md border p-3",
                        f.severidade === "critico" && "border-critico-border bg-critico-bg/60",
                        f.severidade === "atencao" && "border-atencao-border bg-atencao-bg/60",
                      )}
                    >
                      <SeverityIcon severity={f.severidade} className="mt-0.5 size-5 shrink-0" />
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <SeverityBadge severity={f.severidade} />
                          <Badge variant="outline">{FINDING_LABEL[f.tipo]}</Badge>
                        </div>
                        <p className="font-bold">{f.titulo}</p>
                        <p className="text-sm">{f.detalhe}</p>
                        <p className="text-sm">
                          <span className="font-bold">Recomendação: </span>
                          {f.recomendacao}
                        </p>
                        <div className="flex flex-wrap items-center gap-2">
                          <SourceRefs patientId={patient.id} refs={f.refs} />
                          {f.alert_id && (
                            <AckButton
                              alert={alerts[f.alert_id]}
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
              <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
                <FlaskConical aria-hidden className="size-3.5" /> {run.meta.aviso}
              </p>
            </CardContent>
          </Card>

          <section
            aria-label="Hipóteses da IA"
            className="rounded-lg border-2 border-dashed border-primary/50 bg-secondary/40 p-4 space-y-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="ai">
                <Bot aria-hidden /> Hipótese da IA — confirmar
              </Badge>
              {run.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
              {run.meta.modelo && (
                <span className="text-xs text-muted-foreground">
                  {run.meta.modelo} · prompt v{run.meta.prompt_version}
                </span>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              Sintomas novos das últimas 48h compatíveis com efeito adverso ou retirada de medicação. Não são alertas
              determinísticos.
            </p>
            {run.hipoteses.length === 0 ? (
              <p className="text-sm">
                {run.meta.modo ? "Nenhuma hipótese com fonte válida." : "Sem sinais novos ou mudanças relevantes para avaliar."}
              </p>
            ) : (
              <ul className="space-y-2">
                {run.hipoteses.map((h, i) => (
                  <li key={i} className="rounded-md border border-dashed border-primary/40 bg-card p-3 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={h.confidence === "alta" ? "critico" : h.confidence === "media" ? "atencao" : "outline"}>
                        {CONFIDENCE[h.confidence]}
                      </Badge>
                      <Badge variant="outline">
                        {h.mecanismo === "retirada" ? "Retirada" : h.mecanismo === "interacao" ? "Interação" : "Efeito adverso"}
                      </Badge>
                      <span className="text-sm font-bold">{h.medicamento}</span>
                    </div>
                    <p>{reidentify(h.texto, patient.nome)}</p>
                    <SourceRefs patientId={patient.id} refs={h.refs} />
                  </li>
                ))}
              </ul>
            )}
            {run.meta.modo && <FeedbackButtons kind="medrec" entityId={run.id} patientId={patient.id} />}
          </section>

          {(run.prescricoes_propostas?.length ?? 0) > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <ClipboardPen aria-hidden className="size-5 text-primary" /> Prescrições propostas na consulta (
                  {run.prescricoes_propostas?.length})
                </CardTitle>
                <CardDescription>
                  Rascunhos assinados pelo profissional na consulta com IA (status “draft”). Ainda não estão em uso: foram conferidos
                  contra alergias e a prescrição ativa, e os alertas entram na lista acima.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2 text-sm">
                  {run.prescricoes_propostas?.map((p) => (
                    <li key={p.ref} className="rounded-md border p-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <p className="font-bold">{p.nome}</p>
                        <Badge variant="outline">{p.classe_rotulo}</Badge>
                        <Badge variant={p.acao === "substitui" ? "atencao" : "info"}>{p.acao === "substitui" ? "Altera prescrição ativa" : "Nova"}</Badge>
                        {p.alertas.length > 0 && <Badge variant="critico">Alerta crítico</Badge>}
                        <SourceRefs patientId={patient.id} refs={[p.ref, ...(p.substitui ? [p.substitui] : [])]} />
                      </div>
                      <p className="mt-1">{p.texto}</p>
                      {(p.prescritor || p.em) && (
                        <p className="text-xs text-muted-foreground">
                          Proposta por {p.prescritor ?? "profissional"}
                          {p.em ? ` em ${fmtDateTime(p.em)}` : ""}
                          {p.justificativa ? ` · ${p.justificativa}` : ""}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}

          {run.origem && (
            <Card>
              <CardHeader>
                <CardTitle>
                  {run.origem.setor} → {run.destino.setor}
                </CardTitle>
                <CardDescription>Prescrição em uso na saída da origem comparada com a prescrição do destino.</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] border-collapse text-sm">
                    <caption className="sr-only">Comparação de prescrições por princípio ativo</caption>
                    <thead>
                      <tr className="border-b text-left">
                        <th scope="col" className="py-2 pr-3">Medicamento</th>
                        <th scope="col" className="py-2 pr-3">Origem ({run.origem.setor})</th>
                        <th scope="col" className="py-2 pr-3">Destino ({run.destino.setor})</th>
                        <th scope="col" className="py-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {run.linhas.map((r) => {
                        const critical = run.achados.some(
                          (f) => f.severidade === "critico" && f.medicamentos.includes(r.principio_ativo),
                        );
                        return (
                        <tr
                          key={r.principio_ativo}
                          className={cn("border-b align-top", (r.status === "omitida" || critical) && "bg-critico-bg/70")}
                        >
                          <th scope="row" className="py-2 pr-3 text-left font-bold">
                            {r.nome}
                            <span className="block text-xs font-normal text-muted-foreground">{r.classe_rotulo || r.classe.replaceAll("_", " ")}</span>
                          </th>
                          <td className="py-2 pr-3">{r.origem?.texto ?? <span className="text-muted-foreground">—</span>}</td>
                          <td className="py-2 pr-3">
                            {r.destino ? (
                              <>
                                {r.destino.texto}
                                {r.destino.status === "stopped" && (
                                  <span className="block text-xs text-muted-foreground">(suspenso)</span>
                                )}
                              </>
                            ) : (
                              <span className={cn(r.status === "omitida" ? "font-bold text-critico" : "text-muted-foreground")}>
                                {r.status === "omitida" ? "Ausente" : "—"}
                              </span>
                            )}
                          </td>
                          <td className="py-2">
                            <span className="flex flex-wrap gap-1">
                              <Badge variant={STATUS[r.status].variant}>{STATUS[r.status].label}</Badge>
                              {critical && r.status !== "omitida" && <SeverityBadge severity="critico" />}
                            </span>
                            {r.observacao && <p className="mt-1 text-xs text-muted-foreground">{r.observacao}</p>}
                            <SourceRefs patientId={patient.id} refs={r.refs} label="fonte" />
                          </td>
                        </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
