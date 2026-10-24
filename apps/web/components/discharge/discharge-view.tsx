"use client";

import { useEffect, useRef, useState } from "react";
import { Calculator, CircleCheck, ClipboardList, Loader2, Lock, LockOpen, ShieldAlert, Sparkles } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { DischargeDocumentsCard } from "@/components/discharge/discharge-documents";
import { usePatient } from "@/components/patient/patient-context";
import { RiskBadge } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { DictateText } from "@/components/dictate-button";
import { api, type DischargeAssessment, type DischargePrefill, type SocialChecklist } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type BoolField = Exclude<keyof SocialChecklist, "dispositivos" | "observacoes">;

const QUESTIONS: { field: BoolField; label: string; help?: string; showIf?: (c: SocialChecklist) => boolean }[] = [
  { field: "mora_sozinho", label: "Mora sozinho(a)?" },
  { field: "tem_cuidador", label: "Tem cuidador(a) para o pós-alta?" },
  { field: "cuidador_capacitado", label: "O cuidador está capacitado para os cuidados e medicações?", showIf: (c) => c.tem_cuidador === true },
  { field: "escadas_em_casa", label: "Há escadas em casa?" },
  { field: "mobilidade_reduzida", label: "Mobilidade reduzida?" },
  {
    field: "acesso_medicamentos",
    label: "Tem acesso garantido aos medicamentos?",
    help: "Consegue comprar ou retirar no SUS/Farmácia Popular (resposta manual no MVP).",
  },
  { field: "precisa_dispositivos", label: "Precisa de dispositivos em casa?", help: "O2, sonda, curativo complexo, glicosímetro…" },
  { field: "ja_tem_dispositivos", label: "Já tem os dispositivos necessários?", showIf: (c) => c.precisa_dispositivos === true },
  { field: "consulta_retorno_agendada", label: "Consulta de retorno já agendada?" },
  { field: "teach_back_realizado", label: "Entende o plano de alta (teach-back realizado)?" },
];

const DEVICES = ["Oxigênio domiciliar", "Sonda", "Curativo complexo", "Glicosímetro e insumos", "Andador/cadeira de rodas"];

const EMPTY: SocialChecklist = {
  mora_sozinho: null,
  tem_cuidador: null,
  cuidador_capacitado: null,
  escadas_em_casa: null,
  mobilidade_reduzida: null,
  acesso_medicamentos: null,
  precisa_dispositivos: null,
  dispositivos: [],
  ja_tem_dispositivos: null,
  consulta_retorno_agendada: null,
  teach_back_realizado: null,
  observacoes: "",
};

const PRIORITY = { alta: "critico", media: "atencao", baixa: "outline" } as const;
const OWNER: Record<string, string> = {
  medico: "Médico(a)",
  enfermagem: "Enfermagem",
  servico_social: "Serviço social",
  farmacia: "Farmácia",
  fisioterapia: "Fisioterapia",
  equipe: "Equipe",
};

export function DischargeView() {
  const { patient, reload: reloadPatient } = usePatient();
  const { user } = useAuth();
  const isDoctor = user?.role === "medico";
  const [form, setForm] = useState<SocialChecklist>(EMPTY);
  const [prefill, setPrefill] = useState<DischargePrefill | null>(null);
  const [assessment, setAssessment] = useState<DischargeAssessment | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState<Set<BoolField>>(new Set());
  const [justification, setJustification] = useState("");
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api<DischargeAssessment | null>(`/patients/${patient.id}/discharge-assessment/latest`),
      api<DischargePrefill>(`/patients/${patient.id}/discharge-assessment/prefill`),
    ])
      .then(([latest, pre]) => {
        if (!alive) return;
        setPrefill(pre);
        setAssessment(latest);
        setForm(latest ? { ...EMPTY, ...latest.checklist } : { ...EMPTY, ...pre.valores });
      })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [patient.id]);

  const visible = QUESTIONS.filter((q) => !q.showIf || q.showIf(form));

  function setField(field: BoolField, value: boolean) {
    setForm((f) => {
      const next = { ...f, [field]: value };
      if (field === "tem_cuidador" && !value) next.cuidador_capacitado = null;
      if (field === "precisa_dispositivos" && !value) {
        next.ja_tem_dispositivos = null;
        next.dispositivos = [];
      }
      return next;
    });
    setMissing((m) => {
      const n = new Set(m);
      n.delete(field);
      return n;
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const unanswered = visible.filter((q) => form[q.field] === null).map((q) => q.field);
    if (unanswered.length) {
      setMissing(new Set(unanswered));
      document.getElementById(`q-${unanswered[0]}`)?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const a = await api<DischargeAssessment>(`/patients/${patient.id}/discharge-assessment`, {
        method: "POST",
        json: { social_checklist: { ...form, observacoes: form.observacoes || null } },
      });
      setAssessment(a);
      setJustification("");
      void reloadPatient();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro ao calcular o risco");
    } finally {
      setBusy(false);
    }
  }

  async function override() {
    if (!assessment) return;
    if (justification.trim().length < 20) {
      setOverrideError("A justificativa precisa ter pelo menos 20 caracteres.");
      return;
    }
    setBusy(true);
    setOverrideError(null);
    try {
      setAssessment(
        await api<DischargeAssessment>(`/discharge-assessments/${assessment.id}/override`, {
          method: "POST",
          json: { justification },
        }),
      );
      void reloadPatient();
    } catch (err) {
      setOverrideError(err instanceof Error ? err.message : "Erro ao registrar override");
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!assessment) return;
    setBusy(true);
    setError(null);
    try {
      setAssessment(await api<DischargeAssessment>(`/discharge-assessments/${assessment.id}/confirm`, { method: "POST" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro ao confirmar a alta");
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-[480px]" />
        <Skeleton className="h-[480px]" />
      </div>
    );
  }

  const a = assessment;
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <Card className="h-fit">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ClipboardList aria-hidden className="size-5 text-primary" /> Checklist social e domiciliar
          </CardTitle>
          <CardDescription>Respostas sugeridas do prontuário aparecem marcadas — confirme cada uma.</CardDescription>
        </CardHeader>
        <CardContent>
          <form ref={formRef} onSubmit={submit} className="space-y-4" noValidate>
            {visible.map((q) => {
              const source = prefill?.fontes[q.field];
              const value = form[q.field];
              const invalid = missing.has(q.field);
              return (
                <fieldset key={q.field} className="space-y-1.5" aria-describedby={invalid ? `err-${q.field}` : undefined}>
                  <legend className="text-sm font-bold leading-snug">{q.label}</legend>
                  {q.help && <p className="text-xs text-muted-foreground">{q.help}</p>}
                  <div className="flex gap-2" role="radiogroup" aria-label={q.label}>
                    {[true, false].map((opt, i) => (
                      <button
                        key={String(opt)}
                        id={i === 0 ? `q-${q.field}` : undefined}
                        type="button"
                        role="radio"
                        aria-checked={value === opt}
                        onClick={() => setField(q.field, opt)}
                        className={cn(
                          "min-h-11 flex-1 rounded-md border px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          value === opt ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-secondary",
                          invalid && "border-destructive",
                        )}
                      >
                        {opt ? "Sim" : "Não"}
                      </button>
                    ))}
                  </div>
                  {invalid && (
                    <p id={`err-${q.field}`} className="text-xs font-bold text-destructive">
                      Responda esta pergunta.
                    </p>
                  )}
                  {source && (
                    <p className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                      <Sparkles aria-hidden className="size-3.5 text-primary" />
                      Do prontuário: {source.explicacao}
                      {source.ref && <SourceRefs patientId={patient.id} refs={[source.ref]} label="fonte" />}
                    </p>
                  )}
                  {q.field === "precisa_dispositivos" && value === true && (
                    <div className="grid gap-2 pt-1">
                      {DEVICES.map((d) => (
                        <div key={d} className="flex items-center gap-2">
                          <Checkbox
                            id={`dev-${d}`}
                            checked={form.dispositivos.includes(d)}
                            onCheckedChange={(checked) =>
                              setForm((f) => ({
                                ...f,
                                dispositivos: checked ? [...f.dispositivos, d] : f.dispositivos.filter((x) => x !== d),
                              }))
                            }
                          />
                          <Label htmlFor={`dev-${d}`} className="font-normal">
                            {d}
                          </Label>
                        </div>
                      ))}
                    </div>
                  )}
                </fieldset>
              );
            })}
            <div className="space-y-1.5">
              <Label htmlFor="obs">Observações (opcional)</Label>
              <Textarea
                id="obs"
                value={form.observacoes ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, observacoes: e.target.value }))}
              />
            </div>
            <Button type="submit" className="w-full" disabled={busy}>
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Calculator aria-hidden />}
              {a ? "Reavaliar risco" : "Calcular risco de reinternação"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <div className="space-y-4">
        {error && (
          <Alert variant="critico">
            <ShieldAlert aria-hidden />
            {error}
          </Alert>
        )}
        {!a && !busy && (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              Responda o checklist para calcular o risco (LACE do prontuário + fatores sociais) e receber sugestões para o
              plano de alta.
            </CardContent>
          </Card>
        )}
        {busy && !a && <Skeleton className="h-64" />}
        {a && (
          <>
            <Card
              className={cn(
                "border-2",
                a.final_risk === "alto" && "border-critico-border",
                a.final_risk === "moderado" && "border-atencao-border",
                a.final_risk === "baixo" && "border-ok-border",
              )}
            >
              <CardContent className="space-y-3 pt-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-xs font-bold uppercase tracking-wide text-muted-foreground">Risco de reinternação</p>
                    <p className="text-2xl font-bold capitalize">{a.final_risk}</p>
                  </div>
                  <RiskBadge risk={a.final_risk} />
                </div>
                <p className="text-sm">{a.explicacao}</p>
                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge variant="outline">LACE {a.lace.score} ({a.lace.banda})</Badge>
                  <Badge variant="outline">Social {a.social.score} ({a.social.banda})</Badge>
                  <span className="text-muted-foreground">Avaliado em {fmtDateTime(a.created_at)}</span>
                </div>

                {a.confirmada ? (
                  <Alert variant="ok">
                    <CircleCheck aria-hidden />
                    Alta confirmada no HousIA por {a.confirmada.por} em {fmtDateTime(a.confirmada.em)}. (O HousIA não escreve
                    no prontuário.)
                  </Alert>
                ) : (
                  <div className="space-y-3 rounded-md border bg-muted/40 p-3">
                    {a.trava.ativa ? (
                      <Alert variant="critico">
                        <Lock aria-hidden />
                        <div>
                          <p className="font-bold">Alta travada</p>
                          <p id="trava-motivo">{a.trava.motivo}</p>
                        </div>
                      </Alert>
                    ) : (
                      a.final_risk === "alto" && (
                        <Alert variant="atencao">
                          <LockOpen aria-hidden />
                          Trava liberada por override justificado.
                        </Alert>
                      )
                    )}
                    {a.tarefas && a.tarefas.abertas.length > 0 && (
                      <div className="rounded-md border bg-card p-3 text-sm" data-testid="tarefas-abertas">
                        <p className="font-medium">Tarefas abertas do paciente</p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-5">
                          {a.tarefas.abertas.map((t) => (
                            <li key={t.id}>
                              {t.titulo}
                              {t.critica && <span className="font-semibold text-destructive"> (crítica)</span>} · prazo{" "}
                              {new Date(t.prazo).toLocaleDateString("pt-BR")}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="accent"
                        onClick={() => void confirm()}
                        disabled={busy || a.trava.ativa || !isDoctor}
                        aria-describedby={a.trava.ativa ? "trava-motivo" : undefined}
                      >
                        {a.trava.ativa ? <Lock aria-hidden /> : <CircleCheck aria-hidden />}
                        Confirmar alta
                      </Button>
                      {a.trava.ativa && (
                        <Button
                          variant="outline"
                          onClick={() => {
                            formRef.current?.scrollIntoView({ behavior: "smooth" });
                            document.getElementById(`q-${QUESTIONS[0].field}`)?.focus();
                          }}
                        >
                          Aplicar sugestões e reavaliar
                        </Button>
                      )}
                    </div>
                    {!isDoctor && <p className="text-xs text-muted-foreground">Confirmação e override são restritos ao perfil médico.</p>}
                    {a.trava.ativa && isDoctor && (
                      <div className="space-y-1.5">
                        <Label htmlFor="justificativa">Registrar override (justificativa clínica, mín. 20 caracteres)</Label>
                        <Textarea
                          id="justificativa"
                          value={justification}
                          onChange={(e) => {
                            setJustification(e.target.value);
                            setOverrideError(null);
                          }}
                          aria-invalid={overrideError ? true : undefined}
                          aria-describedby="just-count just-err"
                          placeholder="Ex.: filha virá morar com o paciente nas próximas 2 semanas; visita do Melhor em Casa agendada para amanhã."
                        />
                        <div className="flex items-center justify-between gap-2">
                          <p id="just-count" className="text-xs text-muted-foreground tabular-nums">
                            {justification.trim().length}/20 caracteres
                          </p>
                          <DictateText
                            label="justificativa do override"
                            value={justification}
                            onChange={(t) => {
                              setJustification(t);
                              setOverrideError(null);
                            }}
                          />
                          <Button variant="destructive" size="sm" onClick={() => void override()} disabled={busy}>
                            Registrar override
                          </Button>
                        </div>
                        {overrideError && (
                          <p id="just-err" className="text-xs font-bold text-destructive">
                            {overrideError}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                )}
                {a.overrides.length > 0 && (
                  <div className="text-sm">
                    <p className="font-bold">Overrides registrados</p>
                    <ul className="mt-1 space-y-1">
                      {a.overrides.map((o) => (
                        <li key={o.id} className="rounded border bg-card p-2">
                          <span className="text-xs text-muted-foreground">
                            {o.usuario} · {fmtDateTime(o.created_at)}
                          </span>
                          <p>{o.justificativa}</p>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <div className="flex flex-wrap items-center gap-2">
                  <CardTitle>Sugestões para o plano de alta</CardTitle>
                  <AiBadge />
                  {a.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
                </div>
                <CardDescription>
                  {a.meta.modelo} · prompt v{a.meta.prompt_version}. Cada sugestão mostra o dado que a motivou.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {a.sugestoes.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Nenhuma sugestão adicional.</p>
                ) : (
                  <ol className="space-y-2">
                    {a.sugestoes.map((s, i) => (
                      <li key={i} className="rounded-md border p-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={PRIORITY[s.prioridade]}>Prioridade {s.prioridade === "media" ? "média" : s.prioridade}</Badge>
                          <Badge variant="outline">{OWNER[s.responsavel] ?? s.responsavel}</Badge>
                        </div>
                        <p className="mt-1.5">{reidentify(s.texto, patient.nome)}</p>
                        <p className="text-xs text-muted-foreground">Motivo: {reidentify(s.motivo, patient.nome)}</p>
                        <SourceRefs patientId={patient.id} refs={s.refs} />
                      </li>
                    ))}
                  </ol>
                )}
                <div className="mt-3">
                  <FeedbackButtons kind="discharge" entityId={a.id} patientId={patient.id} />
                </div>
              </CardContent>
            </Card>

            <DischargeDocumentsCard />

            <Card>
              <CardHeader>
                <CardTitle>Composição do score (sem caixa-preta)</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <caption className="mb-1 text-left font-bold">LACE — calculado do prontuário</caption>
                    <thead>
                      <tr className="border-b text-left">
                        <th scope="col" className="py-1.5 pr-2">Item</th>
                        <th scope="col" className="py-1.5 pr-2">Valor</th>
                        <th scope="col" className="py-1.5 pr-2 text-right">Pontos</th>
                        <th scope="col" className="py-1.5">Fonte</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(["L", "A", "C", "E"] as const).map((k) => {
                        const c = a.lace.componentes[k];
                        return (
                          <tr key={k} className="border-b align-top">
                            <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                              <span className="font-bold">{k}</span> · {c.rotulo}
                              {c.categorias && c.categorias.length > 0 && (
                                <ul className="mt-1 text-xs text-muted-foreground">
                                  {c.categorias.map((cat) => (
                                    <li key={cat.categoria}>
                                      {cat.categoria} ({cat.cid10}): +{cat.peso}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </th>
                            <td className="py-1.5 pr-2">{c.valor}</td>
                            <td className="py-1.5 pr-2 text-right font-bold tabular-nums">{c.pontos}</td>
                            <td className="py-1.5">
                              <SourceRefs patientId={patient.id} refs={c.refs} label="fonte" />
                            </td>
                          </tr>
                        );
                      })}
                      <tr>
                        <th scope="row" className="py-1.5 pr-2 text-left">Total LACE</th>
                        <td className="py-1.5 pr-2 text-xs text-muted-foreground">&lt;5 baixo · 5–9 moderado · ≥10 alto</td>
                        <td className="py-1.5 pr-2 text-right font-bold tabular-nums">{a.lace.score}</td>
                        <td />
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div>
                  <p className="font-bold text-sm">Score social — pontos do checklist</p>
                  {a.social.itens.length === 0 ? (
                    <p className="text-sm text-muted-foreground">Nenhum fator social de risco marcado.</p>
                  ) : (
                    <ul className="mt-1 divide-y text-sm">
                      {a.social.itens.map((i) => (
                        <li key={i.campo} className="flex justify-between gap-2 py-1">
                          <span>{i.rotulo}</span>
                          <span className="font-bold tabular-nums">+{i.peso}</span>
                        </li>
                      ))}
                      <li className="flex justify-between gap-2 py-1 font-bold">
                        <span>Total social</span>
                        <span className="tabular-nums">{a.social.score}</span>
                      </li>
                    </ul>
                  )}
                  <p className="mt-2 text-xs text-atencao">
                    {a.social.aviso} Faixas: ≥{a.social.faixas.moderado} moderado, ≥{a.social.faixas.alto} alto.
                  </p>
                </div>
                <details className="text-sm">
                  <summary className="cursor-pointer font-bold">Como o risco final é combinado</summary>
                  <ul className="mt-2 list-disc space-y-1 pl-5">
                    <li>
                      <strong>Alto</strong>: social alto, ou LACE alto com social moderado/alto.
                    </li>
                    <li>
                      <strong>Moderado</strong>: LACE alto mitigado por social baixo, ou LACE moderado, ou social moderado.
                    </li>
                    <li>
                      <strong>Baixo</strong>: LACE baixo e social baixo.
                    </li>
                    <li>Risco alto trava a confirmação até reavaliação ou override justificado (registrado e auditado).</li>
                  </ul>
                </details>
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
