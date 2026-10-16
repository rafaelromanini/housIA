"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  BookOpenText,
  Bot,
  Brain,
  CircleCheck,
  ClipboardList,
  FileHeart,
  FlaskConical,
  HeartHandshake,
  History,
  ListChecks,
  MessageCircleQuestion,
  Pill,
  Scissors,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Tags,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import type { Anamnese, ConsultInsights, Origin, ProtocolItem, QueixaDetalhada, SemioField } from "@/lib/api";
import { cn } from "@/lib/utils";

type AppKey =
  | "perguntas"
  | "alertas"
  | "queixas"
  | "emocoes"
  | "resumo"
  | "hipoteses"
  | "anamnese"
  | "cirurgia"
  | "medicamentos"
  | "alergias"
  | "historico"
  | "exames"
  | "protocolos";

const APPS: { key: AppKey; label: string; tab: TabKey }[] = [
  { key: "perguntas", label: "Sugestão de perguntas", tab: "vivo" },
  { key: "alertas", label: "Alertas de segurança", tab: "vivo" },
  { key: "queixas", label: "Queixa principal", tab: "vivo" },
  { key: "emocoes", label: "Análise multidimensional", tab: "vivo" },
  { key: "resumo", label: "Sumarizador", tab: "vivo" },
  { key: "hipoteses", label: "Hipóteses diagnósticas", tab: "raciocinio" },
  { key: "anamnese", label: "Anamnese estruturada", tab: "raciocinio" },
  { key: "cirurgia", label: "Cirurgia", tab: "raciocinio" },
  { key: "medicamentos", label: "Histórico de medicamentos", tab: "medicamentos" },
  { key: "alergias", label: "Alergias", tab: "medicamentos" },
  { key: "historico", label: "Histórico médico", tab: "prontuario" },
  { key: "exames", label: "Exames", tab: "prontuario" },
  { key: "protocolos", label: "Protocolos (atenção primária)", tab: "prontuario" },
];

type TabKey = "vivo" | "raciocinio" | "medicamentos" | "prontuario";
const TABS: { key: TabKey; label: string; short: string }[] = [
  { key: "vivo", label: "Ao vivo", short: "Ao vivo" },
  { key: "raciocinio", label: "Raciocínio clínico", short: "Raciocínio" },
  { key: "medicamentos", label: "Medicamentos e alergias", short: "Medicamentos" },
  { key: "prontuario", label: "Prontuário e exames", short: "Prontuário" },
];
const STORAGE = "housia.smartapps.ocultos";

export function OriginBadge({ origem }: { origem: Origin }) {
  return origem === "ia" ? (
    <Badge variant="ai" className="text-[11px]">
      <Bot aria-hidden /> IA — confirmar
    </Badge>
  ) : (
    <Badge variant="outline" className="text-[11px]">
      <ShieldCheck aria-hidden /> Regra
    </Badge>
  );
}

function AppCard({
  title,
  Icon,
  hint,
  count,
  children,
  className,
}: {
  title: string;
  Icon: typeof Tags;
  hint?: string;
  count?: number;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon aria-hidden className="size-5 text-primary" />
          {title}
          {count !== undefined && <span className="text-sm font-normal text-muted-foreground">({count})</span>}
        </CardTitle>
        {hint && <CardDescription>{hint}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

const Empty = ({ children }: { children: React.ReactNode }) => <p className="text-sm text-muted-foreground">{children}</p>;

const PRIORITY = { alta: "critico", media: "atencao", baixa: "info" } as const;
const PRIORITY_LABEL = { alta: "Prioridade alta", media: "Prioridade média", baixa: "Prioridade baixa" };
const CONFIDENCE = { alta: "critico", media: "atencao", baixa: "info" } as const;
const NEGATIVE = new Set(["ansiedade", "preocupacao", "tristeza", "frustracao", "confusao", "dor"]);
const SEMIO_LABEL: Record<SemioField, string> = {
  inicio_duracao: "Início e duração",
  localizacao: "Localização",
  intensidade: "Intensidade",
  piora: "Piora",
  melhora: "Melhora",
  frequencia: "Frequência",
  associados: "Sintomas associados",
};
const SEMIO_ORDER: SemioField[] = ["inicio_duracao", "localizacao", "intensidade", "piora", "melhora", "frequencia", "associados"];

/** Semiologia de cada queixa: o que já foi dito (com a fala de origem) e o que falta perguntar. */
function Semiologia({
  anamnese,
  pid,
  r,
  onMainComplaint,
}: {
  anamnese: Anamnese;
  pid: string;
  r: (t: string) => string;
  onMainComplaint?: (chave: string | null) => void;
}) {
  const queixas: QueixaDetalhada[] = anamnese.queixas_detalhadas ?? [];
  const extras = anamnese.campos_especialidade ?? [];
  if (queixas.length === 0 && extras.length === 0) return null;
  return (
    <div className="mt-3 space-y-2 border-t pt-3">
      <h4 className="text-sm font-bold">Semiologia por queixa</h4>
      <ul className="space-y-2">
        {queixas.map((q) => {
          const principal = anamnese.qp_principal === q.chave;
          return (
            <li key={q.chave} className="rounded-md border bg-background p-2.5 text-sm">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-bold">{r(q.rotulo)}</span>
                {principal && <Badge variant="outline" className="text-[11px]">{anamnese.qp_fixada ? "Principal (fixada)" : "Principal"}</Badge>}
                {onMainComplaint && !principal && (
                  <Button
                    variant="link"
                    size="sm"
                    className="min-h-9 px-1 text-xs"
                    onClick={() => onMainComplaint(q.chave)}
                    aria-label={`Fixar ${q.rotulo} como queixa principal`}
                  >
                    Fixar como principal
                  </Button>
                )}
                {onMainComplaint && principal && anamnese.qp_fixada && (
                  <Button
                    variant="link"
                    size="sm"
                    className="min-h-9 px-1 text-xs"
                    onClick={() => onMainComplaint(null)}
                    aria-label="Soltar a queixa principal fixada"
                  >
                    Soltar
                  </Button>
                )}
              </div>
              <dl className="mt-1 grid gap-x-3 gap-y-0.5 sm:grid-cols-[max-content_1fr]">
                {SEMIO_ORDER.filter((f) => q[f].valor).map((f) => (
                  <div key={f} className="contents">
                    <dt className="text-muted-foreground">{SEMIO_LABEL[f]}</dt>
                    <dd>
                      {r(q[f].valor)} <SourceRefs patientId={pid} refs={q[f].refs} />
                    </dd>
                  </div>
                ))}
                {q.nega.valor && (
                  <div className="contents">
                    <dt className="text-muted-foreground">Nega</dt>
                    <dd>
                      {r(q.nega.valor)} <SourceRefs patientId={pid} refs={q.nega.refs} />
                    </dd>
                  </div>
                )}
              </dl>
              {q.faltam.length > 0 && (
                <p className="mt-1 text-xs text-muted-foreground">
                  <span className="font-bold">Falta perguntar: </span>
                  {q.faltam.map((f) => SEMIO_LABEL[f].toLowerCase()).join(", ")}
                </p>
              )}
            </li>
          );
        })}
      </ul>
      {extras.length > 0 && (
        <dl className="grid gap-x-3 gap-y-0.5 text-sm sm:grid-cols-[max-content_1fr]">
          {extras.map((c) => (
            <div key={c.campo} className="contents">
              <dt className="text-muted-foreground">{c.rotulo}</dt>
              <dd>
                {c.valor ? (
                  <>
                    {r(c.valor)} <SourceRefs patientId={pid} refs={c.refs} />
                  </>
                ) : (
                  <span className="text-xs text-muted-foreground">Falta perguntar</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

export function SmartAppsPanel({
  insights: ins,
  onMainComplaint,
}: {
  insights: ConsultInsights;
  /** fixa (ou solta, com null) a queixa principal da anamnese; sem ela, a anamnese é só leitura */
  onMainComplaint?: (chave: string | null) => void;
}) {
  const { patient } = usePatient();
  const r = (t: string) => reidentify(t, patient.nome);
  const [tab, setTab] = useState<TabKey>("vivo");
  const [hidden, setHidden] = useState<Set<AppKey>>(new Set());

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE);
      if (saved) setHidden(new Set(JSON.parse(saved) as AppKey[]));
    } catch {
      /* sem armazenamento local */
    }
  }, []);

  function toggle(key: AppKey) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      try {
        window.localStorage.setItem(STORAGE, JSON.stringify([...next]));
      } catch {
        /* sem armazenamento local */
      }
      return next;
    });
  }

  const show = (key: AppKey) => !hidden.has(key) && APPS.find((a) => a.key === key)?.tab === tab;
  const pid = patient.id;
  const negatives = ins.linha_emocional.filter((p) => p.emocoes.some((e) => NEGATIVE.has(e))).length;

  return (
    <section aria-label="Smart Apps" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="tablist" aria-label="Grupos de Smart Apps" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
          {TABS.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              aria-label={t.label}
              title={t.label}
              onClick={() => setTab(t.key)}
              className={cn(
                "min-h-10 rounded-md px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                tab === t.key ? "bg-card text-primary shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
            >
              <span className="xl:hidden">{t.short}</span>
              <span className="hidden xl:inline">{t.label}</span>
              {t.key === "vivo" && ins.perguntas.length > 0 && (
                <span className="ml-1.5 rounded-full bg-primary px-1.5 text-xs text-primary-foreground">{ins.perguntas.length}</span>
              )}
            </button>
          ))}
        </div>
        <details className="relative">
          <summary className="inline-flex min-h-10 cursor-pointer items-center gap-1.5 rounded-md border bg-card px-3 text-sm font-bold">
            <Settings2 aria-hidden className="size-4" /> Smart Apps
          </summary>
          <div className="absolute right-0 z-20 mt-1 w-72 space-y-1 rounded-md border bg-card p-3 shadow-lg">
            <p className="mb-1 text-xs text-muted-foreground">Escolha o que aparece durante a consulta.</p>
            {APPS.map((a) => (
              <label key={a.key} className="flex min-h-9 items-center gap-2 text-sm">
                <Checkbox checked={!hidden.has(a.key)} onCheckedChange={() => toggle(a.key)} />
                {a.label}
              </label>
            ))}
          </div>
        </details>
      </div>

      {show("perguntas") && (
        <AppCard
          title="Sugestão de perguntas"
          Icon={MessageCircleQuestion}
          count={ins.perguntas.length}
          hint="Atualiza a cada fala. Some da lista quando o profissional já perguntou, mesmo com outras palavras."
          className="border-primary/40"
        >
          {ins.perguntas.length === 0 ? (
            <Empty>Nada pendente por enquanto.</Empty>
          ) : (
            <ol className="space-y-2">
              {ins.perguntas.map((q) => (
                <li key={q.pergunta} className="rounded-md border bg-background p-2.5">
                  <p className="text-base font-bold leading-snug">“{r(q.pergunta)}”</p>
                  <p className="mt-0.5 text-sm text-muted-foreground">{r(q.motivo)}</p>
                  {q.discrimina && q.discrimina.length > 0 && (
                    <p className="mt-0.5 text-xs font-bold text-primary">Ajuda a decidir entre: {q.discrimina.map(r).join(" × ")}</p>
                  )}
                  {q.fonte && <p className="mt-0.5 text-xs text-muted-foreground">Fonte: {q.fonte}</p>}
                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <Badge variant={PRIORITY[q.prioridade]} className="text-[11px]">
                      {PRIORITY_LABEL[q.prioridade]}
                    </Badge>
                    <OriginBadge origem={q.origem} />
                    <SourceRefs patientId={pid} refs={q.refs} label="motivo no prontuário" />
                  </div>
                </li>
              ))}
            </ol>
          )}
          {ins.perguntas_abordadas.length > 0 && (
            <details className="mt-2 text-sm">
              <summary className="min-h-9 cursor-pointer font-bold text-ok">
                <CircleCheck aria-hidden className="mr-1 inline size-4" />
                Já abordadas na conversa ({ins.perguntas_abordadas.length})
              </summary>
              <ul className="mt-1 space-y-1">
                {ins.perguntas_abordadas.map((q) => (
                  <li key={q.id ?? q.pergunta} className="text-muted-foreground">
                    <span className="flex flex-wrap items-center gap-1">
                      <CircleCheck aria-hidden className="size-3.5 text-ok" /> {r(q.pergunta)}
                      <SourceRefs patientId={pid} refs={q.refs} label="pergunta feita" />
                      {q.origem === "ia" && <OriginBadge origem="ia" />}
                    </span>
                    {q.fonte && <span className="ml-5 block text-xs">Fonte: {q.fonte}</span>}
                    <span className="ml-5 flex flex-wrap items-center gap-1 text-foreground">
                      {q.resposta ? (
                        <>
                          <span className="font-bold">Resposta:</span> “{r(q.resposta)}”
                          <SourceRefs patientId={pid} refs={q.resposta_refs ?? []} />
                        </>
                      ) : (
                        <span className="text-xs text-muted-foreground">Ainda sem resposta do paciente.</span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </AppCard>
      )}

      {show("alertas") && ins.sugestoes_medicamentos.some((m) => m.severidade === "critico") && (
        <AppCard title="Alertas de segurança" Icon={ShieldAlert} hint="Regras determinísticas: rodam sempre, antes da IA.">
          <ul className="space-y-2">
            {ins.sugestoes_medicamentos
              .filter((m) => m.severidade === "critico")
              .map((m) => (
                <li key={m.texto} className="flex gap-2 rounded-md border border-critico-border bg-critico-bg p-2 text-sm">
                  <SeverityIcon severity="critico" className="mt-0.5 size-5 shrink-0" />
                  <div>
                    <p className="font-bold text-critico">{r(m.texto)}</p>
                    <SourceRefs patientId={pid} refs={m.refs} />
                  </div>
                </li>
              ))}
          </ul>
        </AppCard>
      )}

      {show("queixas") && (
        <AppCard title="Queixa principal" Icon={Tags} hint="Queixas do paciente em tags; riscadas = negativas pertinentes.">
          {ins.queixas.length === 0 ? (
            <Empty>Ainda sem queixas na conversa.</Empty>
          ) : (
            <ul className="flex flex-wrap gap-1.5">
              {ins.queixas.map((t) => (
                <li key={t.rotulo + t.status}>
                  <span
                    className={cn(
                      "inline-flex min-h-9 items-center gap-1 rounded-full border px-3 text-sm font-bold",
                      t.status === "presente" ? "border-atencao-border bg-atencao-bg text-atencao" : "bg-muted text-muted-foreground line-through",
                    )}
                    title={t.status === "negado" ? "Negado pelo paciente" : "Relatado"}
                  >
                    {t.status === "negado" && <span className="sr-only">Nega: </span>}
                    <span>{r(t.rotulo)}</span>
                    {t.detalhe && t.status === "presente" && (
                      <span className="text-xs font-normal">
                        <span aria-hidden> · </span>
                        <span className="sr-only">, </span>
                        {r(t.detalhe)}
                      </span>
                    )}
                    {t.origem === "ia" && <Bot aria-label="sugerido pela IA" className="size-3.5" />}
                  </span>
                  <SourceRefs patientId={pid} refs={t.refs} />
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("emocoes") && (
        <AppCard
          title="Análise multidimensional"
          Icon={HeartHandshake}
          hint="Emoções percebidas na fala do paciente e do acompanhante, com sugestão de abordagem."
        >
          {ins.emocoes.length === 0 ? (
            <Empty>Nenhum sinal emocional explícito até agora.</Empty>
          ) : (
            <ul className="space-y-2">
              {ins.emocoes.map((e) => (
                <li key={e.emocao + e.quem} className="rounded-md border p-2 text-sm">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant={NEGATIVE.has(e.emocao) ? "atencao" : "ok"}>{e.rotulo}</Badge>
                    <span className="text-xs text-muted-foreground">
                      {e.quem === "acompanhante" ? "acompanhante" : "paciente"} · intensidade {e.intensidade}
                    </span>
                    <OriginBadge origem={e.origem} />
                  </div>
                  <p className="mt-1 italic">{e.trecho}</p>
                  <p className="mt-1">
                    <span className="font-bold">Como abordar: </span>
                    {e.abordagem}
                  </p>
                  <SourceRefs patientId={pid} refs={e.refs} />
                </li>
              ))}
            </ul>
          )}
          {ins.linha_emocional.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-bold text-muted-foreground">Ao longo da conversa (falas do paciente/acompanhante)</p>
              <div
                className="mt-1 flex flex-wrap gap-0.5"
                role="img"
                aria-label={`${negatives} fala(s) com sinal de desconforto e ${ins.linha_emocional.filter((p) => p.emocoes.some((x) => !NEGATIVE.has(x))).length} com sinal positivo`}
              >
                {ins.linha_emocional.map((p) => {
                  const neg = p.emocoes.some((x) => NEGATIVE.has(x));
                  const pos = p.emocoes.some((x) => !NEGATIVE.has(x));
                  return (
                    <span
                      key={p.n}
                      title={`fala ${p.n}: ${p.emocoes.join(", ") || "neutra"}`}
                      className={cn("h-4 w-3 rounded-sm", neg ? "bg-atencao" : pos ? "bg-ok" : "bg-border")}
                    />
                  );
                })}
              </div>
              <p className="mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <span className="size-2.5 rounded-sm bg-atencao" aria-hidden /> desconforto
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="size-2.5 rounded-sm bg-ok" aria-hidden /> alívio/confiança
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="size-2.5 rounded-sm bg-border" aria-hidden /> neutra
                </span>
              </p>
            </div>
          )}
          {ins.comunicacao && ins.comunicacao.falas > 0 && (
            <div className="mt-3 space-y-1 text-sm">
              <p className="text-xs font-bold text-muted-foreground">Comunicação</p>
              <div className="flex h-3 gap-0.5 overflow-hidden rounded-full" aria-hidden>
                <span className="rounded-l-full bg-primary" style={{ width: `${ins.comunicacao.palavras_paciente_pct}%` }} />
                <span className="rounded-r-full bg-primary/30" style={{ width: `${ins.comunicacao.palavras_profissional_pct}%` }} />
              </div>
              <p className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <span className="size-2.5 rounded-sm bg-primary" aria-hidden /> paciente/acompanhante
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="size-2.5 rounded-sm bg-primary/30" aria-hidden /> profissional
                </span>
              </p>
              <p>
                Paciente/acompanhante falaram {ins.comunicacao.palavras_paciente_pct}% das palavras · profissional{" "}
                {ins.comunicacao.palavras_profissional_pct}%. Perguntas abertas: {ins.comunicacao.perguntas_abertas} · fechadas:{" "}
                {ins.comunicacao.perguntas_fechadas}.
              </p>
              {ins.comunicacao.perguntas_fechadas > 2 * Math.max(1, ins.comunicacao.perguntas_abertas) && (
                <p className="text-xs text-muted-foreground">
                  Dica: perguntas abertas (“Como…?”, “Me conta…”) ajudam o paciente a trazer o que mais preocupa.
                </p>
              )}
            </div>
          )}
        </AppCard>
      )}

      {show("resumo") && ins.resumo && (
        <AppCard title="Sumarizador" Icon={BookOpenText} hint="Resumo dinâmico com os pontos críticos da consulta até aqui.">
          <p className="text-sm leading-relaxed">{r(ins.resumo.texto)}</p>
          {ins.resumo.destaques.length > 0 && (
            <ul className="mt-2 space-y-1">
              {ins.resumo.destaques.map((h) => (
                <li key={h.texto} className="flex items-start gap-1.5 text-sm">
                  <SeverityIcon severity={h.severidade} className="mt-0.5 size-4 shrink-0" />
                  <span className="flex-1">
                    {r(h.texto)} <SourceRefs patientId={pid} refs={h.refs} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("hipoteses") && (
        <AppCard
          title="Possíveis hipóteses diagnósticas"
          Icon={Brain}
          count={ins.hipoteses.length}
          hint="Apoio ao raciocínio com evidências da conversa e do prontuário. A decisão é do profissional."
        >
          {ins.hipoteses.length === 0 ? (
            <Empty>Ainda sem elementos suficientes.</Empty>
          ) : (
            <ul className="space-y-2">
              {ins.hipoteses.map((h) => (
                <li key={h.hipotese + h.cid} className={cn("rounded-md border p-2.5 text-sm", h.origem === "ia" && "border-dashed")}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="font-bold">{r(h.hipotese)}</p>
                    <Badge variant="outline" className="font-mono text-[11px]">
                      CID {h.cid}
                    </Badge>
                    <Badge variant={CONFIDENCE[h.confianca]} className="text-[11px]">
                      confiança {h.confianca}
                    </Badge>
                    <OriginBadge origem={h.origem} />
                  </div>
                  <ul className="mt-1 space-y-0.5">
                    {h.a_favor.map((e) => (
                      <li key={e.texto} className="flex flex-wrap items-center gap-1">
                        <span className="font-bold text-ok">+</span> {r(e.texto)}
                        <SourceRefs patientId={pid} refs={e.refs} />
                      </li>
                    ))}
                    {h.contra.map((e) => (
                      <li key={e.texto} className="flex flex-wrap items-center gap-1 text-muted-foreground">
                        <span className="font-bold">−</span> {r(e.texto)}
                        <SourceRefs patientId={pid} refs={e.refs} />
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1">
                    <span className="font-bold">Próximos passos: </span>
                    {r(h.proximos_passos)}
                  </p>
                  {h.sinais_de_alarme && (
                    <p className="mt-0.5 text-critico">
                      <span className="font-bold">Sinais de alarme: </span>
                      {r(h.sinais_de_alarme)}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("anamnese") && ins.anamnese && (
        <AppCard title="Anamnese estruturada" Icon={ClipboardList} hint="Organizada a partir do que foi dito, em tempo real.">
          <dl className="space-y-2 text-sm">
            <div>
              <dt className="font-bold">Queixa principal</dt>
              <dd>{r(ins.anamnese.queixa_principal)}</dd>
            </div>
            <div>
              <dt className="font-bold">História da doença atual</dt>
              <dd>{r(ins.anamnese.hda)}</dd>
            </div>
            {(
              [
                ["Antecedentes", ins.anamnese.antecedentes],
                ["Medicações em uso", ins.anamnese.medicacoes_em_uso],
                ["Alergias", ins.anamnese.alergias],
                ["Hábitos e contexto", ins.anamnese.habitos_e_contexto],
                ["Revisão de sistemas", ins.anamnese.revisao_de_sistemas],
              ] as const
            ).map(([label, items]) =>
              items.length ? (
                <div key={label}>
                  <dt className="font-bold">{label}</dt>
                  <dd>
                    <ul className="list-disc pl-5">
                      {items.map((x) => (
                        <li key={x}>{r(x)}</li>
                      ))}
                    </ul>
                  </dd>
                </div>
              ) : null,
            )}
          </dl>
          <Semiologia anamnese={ins.anamnese} pid={pid} r={r} onMainComplaint={onMainComplaint} />
          <SourceRefs patientId={pid} refs={ins.anamnese.refs} />
        </AppCard>
      )}

      {show("cirurgia") && (
        <AppCard title="Cirurgia" Icon={Scissors} hint="Histórico cirúrgico, cirurgias citadas ou planejadas e checagens pré-operatórias.">
          {ins.cirurgia.length === 0 ? (
            <Empty>Sem cirurgias no prontuário ou na conversa.</Empty>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {ins.cirurgia.map((c) => (
                <li key={c.texto} className="flex flex-wrap items-center gap-1.5">
                  <Badge variant={c.tipo === "checagem" ? "info" : c.tipo === "planejada" ? "atencao" : "outline"} className="text-[11px]">
                    {{ historico: "Histórico", planejada: "Planejada/citada", checagem: "Checagem" }[c.tipo]}
                  </Badge>
                  <span className="flex-1">{r(c.texto)}</span>
                  {c.origem === "ia" && <OriginBadge origem="ia" />}
                  <SourceRefs patientId={pid} refs={c.refs} />
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("medicamentos") && (
        <AppCard
          title="Histórico de medicamentos"
          Icon={Pill}
          hint={`Ajustes de dose, alternativas e interações — inclusive com o que o paciente contou que toma.${ins.tfg ? ` TFG estimada: ${Math.round(ins.tfg)} mL/min/1,73 m².` : ""}`}
        >
          {ins.sugestoes_medicamentos.length > 0 && (
            <ul className="mb-3 space-y-1.5">
              {ins.sugestoes_medicamentos.map((m) => (
                <li key={m.texto} className="flex gap-2 text-sm">
                  <SeverityIcon severity={m.severidade} className="mt-0.5 size-4 shrink-0" />
                  <span className="flex-1">
                    {r(m.texto)} {m.origem === "ia" && <OriginBadge origem="ia" />} <SourceRefs patientId={pid} refs={m.refs} />
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Medicamentos em uso, suspensos recentemente e relatados</caption>
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-1 pr-2">Medicamento</th>
                  <th scope="col" className="py-1 pr-2">Posologia</th>
                  <th scope="col" className="py-1">Situação</th>
                </tr>
              </thead>
              <tbody>
                {ins.medicamentos.map((m) => (
                  <tr key={m.refs[0] + m.medicamento} className="border-b align-top">
                    <th scope="row" className="py-1.5 pr-2 text-left font-bold">
                      {m.medicamento}
                      {m.desde && <span className="block text-xs font-normal text-muted-foreground">desde {m.desde}</span>}
                    </th>
                    <td className="py-1.5 pr-2">
                      {m.posologia}
                      {m.observacao && <span className="block text-xs text-muted-foreground">{m.observacao}</span>}
                    </td>
                    <td className="py-1.5">
                      <Badge variant={m.status === "em_uso" ? "ok" : m.status === "relatado" ? "atencao" : "outline"} className="text-[11px]">
                        {{ em_uso: "Em uso", suspenso: "Suspenso", relatado: "Relatado" }[m.status]}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AppCard>
      )}

      {show("alergias") && (
        <AppCard title="Alergias" Icon={ShieldAlert} hint="Do prontuário e as relatadas na conversa, com reatividade cruzada e cuidados práticos.">
          {ins.alergias.length === 0 ? (
            <Empty>Sem alergias registradas ou relatadas.</Empty>
          ) : (
            <ul className="space-y-2">
              {ins.alergias.map((a) => (
                <li key={a.alergeno} className="rounded-md border border-critico-border p-2.5 text-sm">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="font-bold text-critico">{a.alergeno}</p>
                    <Badge variant={a.fonte === "prontuario" ? "outline" : "atencao"} className="text-[11px]">
                      {a.fonte === "prontuario" ? "No prontuário" : "Relatada — registrar no prontuário"}
                    </Badge>
                  </div>
                  <p>{a.reacao}</p>
                  {a.conflitos.map((c) => (
                    <p key={c} className="mt-1 font-bold text-critico">
                      Conflito: {c}
                    </p>
                  ))}
                  <dl className="mt-1 grid gap-0.5">
                    <div>
                      <dt className="inline font-bold">Reatividade cruzada: </dt>
                      <dd className="inline">{a.reatividade_cruzada}</dd>
                    </div>
                    <div>
                      <dt className="inline font-bold">Evitar: </dt>
                      <dd className="inline">{a.evitar}</dd>
                    </div>
                    <div>
                      <dt className="inline font-bold">Alternativas: </dt>
                      <dd className="inline">{a.alternativas}</dd>
                    </div>
                    <div>
                      <dt className="inline font-bold">Dicas: </dt>
                      <dd className="inline">{a.dicas}</dd>
                    </div>
                  </dl>
                  <SourceRefs patientId={pid} refs={a.refs} />
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("historico") && (
        <AppCard title="Histórico médico" Icon={History} hint="Diagnósticos com data de início, cirurgias, consultas e idas ao PS — e o que foi relatado.">
          <ul className="space-y-1 text-sm">
            {ins.historico.map((h) => (
              <li key={h.texto} className="flex flex-wrap items-center gap-1.5">
                <Badge variant={h.tipo === "familiar" || h.tipo === "relatado" ? "atencao" : "outline"} className="text-[11px]">
                  {
                    {
                      diagnostico: "Diagnóstico",
                      cirurgia: "Cirurgia",
                      internacao: "Internação",
                      pronto_socorro: "Pronto-socorro",
                      consulta: "Consulta",
                      familiar: "Familiar",
                      relatado: "Relatado",
                    }[h.tipo]
                  }
                </Badge>
                <span className="flex-1">{r(h.texto)}</span>
                <SourceRefs patientId={pid} refs={h.refs} />
              </li>
            ))}
          </ul>
        </AppCard>
      )}

      {show("exames") && (
        <AppCard title="Exames em linguagem simples" Icon={FlaskConical} hint="Para explicar ao paciente: o que é, o resultado e a tendência.">
          {ins.exames.length === 0 ? (
            <Empty>Sem exames recentes.</Empty>
          ) : (
            <ul className="space-y-2">
              {ins.exames.map((e) => (
                <li key={e.chave} className="rounded-md border p-2 text-sm">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="font-bold">{e.exame}</p>
                    <span className="tabular-nums">{e.valor}</span>
                    <Badge variant={e.status === "normal" ? "ok" : "atencao"} className="text-[11px]">
                      {{ baixo: "Baixo", normal: "Normal", alto: "Alto" }[e.status]}
                    </Badge>
                    <span className="text-xs text-muted-foreground">{e.data}</span>
                    {e.tendencia && (
                      <span className="inline-flex items-center gap-0.5 text-xs text-muted-foreground">
                        {e.tendencia.startsWith("subiu") ? (
                          <TrendingUp aria-hidden className="size-3.5" />
                        ) : e.tendencia.startsWith("caiu") ? (
                          <TrendingDown aria-hidden className="size-3.5" />
                        ) : (
                          <Activity aria-hidden className="size-3.5" />
                        )}
                        {e.tendencia}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5">{e.explicacao}</p>
                  <SourceRefs patientId={pid} refs={e.refs} />
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("protocolos") && <ProtocolsCard items={ins.protocolos} patientId={pid} />}
    </section>
  );
}

const PROTOCOL_STATUS: Record<ProtocolItem["status"], { label: string; variant: "ok" | "atencao" | "critico" | "outline" }> = {
  em_dia: { label: "Em dia", variant: "ok" },
  no_alvo: { label: "No alvo", variant: "ok" },
  pendente: { label: "Pendente", variant: "atencao" },
  sem_registro: { label: "Sem registro", variant: "atencao" },
  fora_do_alvo: { label: "Fora do alvo", variant: "critico" },
};

function ProtocolsCard({ items, patientId }: { items: ProtocolItem[]; patientId: string }) {
  const groups = [...new Set(items.map((i) => i.protocolo))];
  return (
    <AppCard
      title="Protocolos de acompanhamento"
      Icon={ListChecks}
      hint="Linha de cuidado (atenção primária) checada no prontuário. Base de demonstração."
    >
      {items.length === 0 ? (
        <Empty>Nenhum protocolo se aplica aos diagnósticos ativos.</Empty>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <div key={g}>
              <p className="flex items-center gap-1.5 text-sm font-bold">
                <FileHeart aria-hidden className="size-4 text-primary" /> {g}
              </p>
              <ul className="mt-1 space-y-1">
                {items
                  .filter((i) => i.protocolo === g)
                  .map((i) => (
                    <li key={i.item} className="flex flex-wrap items-center gap-1.5 text-sm">
                      <Badge variant={PROTOCOL_STATUS[i.status].variant} className="text-[11px]">
                        {PROTOCOL_STATUS[i.status].label}
                      </Badge>
                      <span className="font-bold">{i.item}</span>
                      <span className="text-xs text-muted-foreground">{i.ultimo ?? i.orientacao}</span>
                      <SourceRefs patientId={patientId} refs={i.refs} />
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </AppCard>
  );
}

