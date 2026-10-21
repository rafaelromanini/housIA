"use client";

import { useEffect, useState } from "react";
import {
  Activity,
  BellRing,
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
  Pin,
  Scissors,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Tags,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { ItemActions, NewBadge, isNewItem, type OnInsightAction } from "@/components/consult/insight-actions";
import { OrdersCard } from "@/components/consult/orders-card";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { getSmartAppProfile, getSmartApps, saveSmartAppProfile } from "@/lib/api";
import type {
  Anamnese,
  AppResult,
  Conciliation,
  ConsultInsights,
  HypothesisDecision,
  Origin,
  ProtocolItem,
  QueixaDetalhada,
  Rcri,
  SemioField,
  SmartAppTab,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** id de um Smart App do registro (GET /smart-apps); os de painel próprio têm o card escrito abaixo */
type AppKey = string;

/** Catálogo de reserva: vale só se o servidor não responder. A fonte de verdade é o registro (GET /smart-apps). */
const FALLBACK_APPS: CatalogApp[] = [
  { id: "perguntas", nome: "Sugestão de perguntas", aba: "vivo" },
  { id: "alertas", nome: "Alertas de segurança", aba: "vivo" },
  { id: "lembretes", nome: "Lembretes", aba: "vivo" },
  { id: "queixas", nome: "Queixa principal", aba: "vivo" },
  { id: "emocoes", nome: "Análise multidimensional", aba: "vivo" },
  { id: "resumo", nome: "Sumarizador", aba: "vivo" },
  { id: "hipoteses", nome: "Hipóteses diagnósticas", aba: "raciocinio" },
  { id: "anamnese", nome: "Anamnese estruturada", aba: "raciocinio" },
  { id: "cirurgia", nome: "Cirurgia", aba: "raciocinio" },
  { id: "medicamentos", nome: "Histórico de medicamentos", aba: "medicamentos" },
  { id: "alergias", nome: "Alergias", aba: "medicamentos" },
  { id: "historico", nome: "Histórico médico", aba: "prontuario" },
  { id: "exames", nome: "Exames", aba: "prontuario" },
  { id: "protocolos", nome: "Protocolos (atenção primária)", aba: "prontuario" },
  { id: "pedidos", nome: "Pedidos em rascunho", aba: "pedidos" },
];
/** apps com card próprio (ações, semiologia, RCRI...); os demais são desenhados pelo tipo de card do manifest */
const OWN_CARD = new Set(FALLBACK_APPS.map((a) => a.id));

type TabKey = SmartAppTab;
type CatalogApp = { id: AppKey; nome: string; aba: TabKey };
const TABS: { key: TabKey; label: string; short: string }[] = [
  { key: "vivo", label: "Ao vivo", short: "Ao vivo" },
  { key: "raciocinio", label: "Raciocínio clínico", short: "Raciocínio" },
  { key: "medicamentos", label: "Medicamentos e alergias", short: "Medicamentos" },
  { key: "prontuario", label: "Prontuário e exames", short: "Prontuário" },
  { key: "transicao", label: "Transição de cuidado", short: "Transição" },
  { key: "pedidos", label: "Pedidos e receitas", short: "Pedidos" },
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
/** perguntas sugeridas abertas na lista; o resto fica em "Mais N perguntas" para o painel caber no tablet */
const VISIBLE_QUESTIONS = 6;
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

const DECISION_LABEL: Record<HypothesisDecision, string> = {
  pendente: "",
  confirmada: "Confirmada",
  provisoria: "Provisória",
  descartada: "Descartada",
};

export function SmartAppsPanel({
  insights: ins,
  onMainComplaint,
  orders,
  onHypothesisDecision,
  onInsightAction,
}: {
  insights: ConsultInsights;
  /** fixa (ou solta, com null) a queixa principal da anamnese; sem ela, a anamnese é só leitura */
  onMainComplaint?: (chave: string | null) => void;
  /** assinatura dos pedidos em rascunho; sem ela, a aba "Pedidos" não aparece (a revisão da nota os assina) */
  orders?: { consultId: number; onInsights: (insights: ConsultInsights) => void };
  /** confirma, deixa provisória, descarta ou limpa a decisão sobre uma hipótese; sem ela, as hipóteses são só leitura */
  onHypothesisDecision?: (cid: string, decisao: HypothesisDecision) => void;
  /** aceitar, descartar (com motivo), marcar como feita ou fixar um insight; sem ela, os botões não aparecem */
  onInsightAction?: OnInsightAction;
}) {
  const { patient } = usePatient();
  const r = (t: string) => reidentify(t, patient.nome);
  const [tab, setTab] = useState<TabKey>("vivo");
  const [hidden, setHidden] = useState<Set<AppKey>>(new Set());
  const [catalog, setCatalog] = useState<CatalogApp[]>(FALLBACK_APPS);
  const specialty = ins.especialidade;

  // catálogo e perfil vêm do servidor (valem em qualquer aparelho); o localStorage antigo é migrado uma única vez
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [list, profile] = await Promise.all([getSmartApps(specialty), getSmartAppProfile(specialty)]);
        if (!alive) return;
        setCatalog(list.map((a) => ({ id: a.id, nome: a.nome, aba: a.aba })));
        let ocultos = profile.ocultos;
        if (profile.origem === "padrao") {
          try {
            const saved = window.localStorage.getItem(STORAGE);
            const legacy = saved ? (JSON.parse(saved) as string[]).filter((id) => list.some((a) => a.id === id)) : [];
            if (legacy.length > 0) ocultos = (await saveSmartAppProfile(specialty, legacy)).ocultos;
            window.localStorage.removeItem(STORAGE);
          } catch {
            /* sem armazenamento local */
          }
        }
        if (alive) setHidden(new Set(ocultos));
      } catch {
        /* sem o servidor, o catálogo de reserva mostra tudo */
      }
    })();
    return () => {
      alive = false;
    };
  }, [specialty]);

  function toggle(key: AppKey) {
    const next = new Set(hidden);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setHidden(next);
    saveSmartAppProfile(specialty, [...next]).catch(() => setHidden(hidden));
  }

  const show = (key: AppKey) => !hidden.has(key) && catalog.find((a) => a.id === key)?.aba === tab;
  // apps do registro sem card próprio: o servidor manda os itens e a tela desenha pelo tipo de card
  const results = new Map((ins.apps ?? []).map((a) => [a.id, a]));
  const generic = catalog.filter((a) => !OWN_CARD.has(a.id) && results.has(a.id));
  const tabHasApps = (t: TabKey) => catalog.some((a) => a.aba === t && (OWN_CARD.has(a.id) || results.has(a.id)));
  const pid = patient.id;

  // uma pergunta sugerida: o texto, o motivo, o que ajuda a decidir, a diretriz de origem e a prioridade
  const questionItem = (q: ConsultInsights["perguntas"][number]) => (
    <li key={q.id ?? q.pergunta} className="rounded-md border bg-background p-2.5">
      <p className="text-base font-bold leading-snug">
        {q.fixado && <Pin aria-label="Fixada no topo" className="mr-1 inline size-4 text-primary" />}“{r(q.pergunta)}”
      </p>
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
        <NewBadge item={q} falas={ins.falas_analisadas} />
        <SourceRefs patientId={pid} refs={q.refs} label="motivo no prontuário" />
      </div>
      <ItemActions item={q} label={q.pergunta} actions={["feita", "descartar"]} onAction={onInsightAction} />
    </li>
  );

  const toDecide = (ins.pedidos ?? []).filter((o) => o.decisao.estado === "pendente").length;
  const negatives = ins.linha_emocional.filter((p) => p.emocoes.some((e) => NEGATIVE.has(e))).length;

  return (
    <section aria-label="Smart Apps" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="tablist" aria-label="Grupos de Smart Apps" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
          {TABS.filter((t) => (orders || t.key !== "pedidos") && tabHasApps(t.key)).map((t) => (
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
              {t.key === "pedidos" && toDecide > 0 && (
                <span className="ml-1.5 rounded-full bg-primary px-1.5 text-xs text-primary-foreground">{toDecide}</span>
              )}
            </button>
          ))}
        </div>
        <details className="relative">
          <summary className="inline-flex min-h-10 cursor-pointer items-center gap-1.5 rounded-md border bg-card px-3 text-sm font-bold">
            <Settings2 aria-hidden className="size-4" /> Smart Apps
          </summary>
          <div className="absolute right-0 z-20 mt-1 w-72 space-y-1 rounded-md border bg-card p-3 shadow-lg">
            <p className="mb-1 text-xs text-muted-foreground">Escolha o que aparece durante a consulta. Fica salvo na sua conta, em qualquer aparelho.</p>
            {catalog
              .filter((a) => OWN_CARD.has(a.id) || results.has(a.id))
              .map((a) => (
                <label key={a.id} className="flex min-h-9 items-center gap-2 text-sm">
                  <Checkbox checked={!hidden.has(a.id)} onCheckedChange={() => toggle(a.id)} />
                  {a.nome}
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
            <>
              <ol className="space-y-2">{ins.perguntas.slice(0, VISIBLE_QUESTIONS).map((q) => questionItem(q))}</ol>
              {ins.perguntas.length > VISIBLE_QUESTIONS && (
                <details className="mt-2 text-sm">
                  <summary className="min-h-9 cursor-pointer font-bold text-primary">
                    Mais {ins.perguntas.length - VISIBLE_QUESTIONS} perguntas
                  </summary>
                  <ol start={VISIBLE_QUESTIONS + 1} className="mt-1 space-y-2">
                    {ins.perguntas.slice(VISIBLE_QUESTIONS).map((q) => questionItem(q))}
                  </ol>
                </details>
              )}
            </>
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

      {show("alertas") && ins.alertas.length > 0 && <AlertsCard ins={ins} pid={pid} r={r} onAction={onInsightAction} />}

      {show("lembretes") && (ins.lembretes ?? []).length > 0 && (
        <AppCard
          title="Lembretes"
          Icon={BellRing}
          count={(ins.lembretes ?? []).length}
          hint="Assuntos que ainda não apareceram na conversa. Cada lembrete some quando o assunto é tratado."
        >
          <ul className="space-y-1.5">
            {(ins.lembretes ?? []).map((l) => (
              <li key={l.chave} className="flex gap-2 text-sm">
                <SeverityIcon severity={l.severidade} className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">
                  {r(l.texto)} <SourceRefs patientId={pid} refs={l.refs} />
                  {l.fonte && <span className="block text-xs text-muted-foreground">Fonte: {l.fonte}</span>}
                </span>
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
                    {t.fixado && <Pin aria-label="Fixada no topo" className="size-3.5" />}
                    <NewBadge item={t} falas={ins.falas_analisadas} />
                  </span>
                  <SourceRefs patientId={pid} refs={t.refs} />
                  <ItemActions item={t} label={t.rotulo} actions={["descartar"]} onAction={onInsightAction} />
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
                    {e.fixado && <Pin aria-label="Fixada no topo" className="size-3.5" />}
                    <NewBadge item={e} falas={ins.falas_analisadas} />
                  </div>
                  <p className="mt-1 italic">{e.trecho}</p>
                  <p className="mt-1">
                    <span className="font-bold">Como abordar: </span>
                    {e.abordagem}
                  </p>
                  <SourceRefs patientId={pid} refs={e.refs} />
                  <ItemActions item={e} label={e.rotulo} actions={["descartar"]} onAction={onInsightAction} />
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
          {(ins.resumo.frases ?? []).length > 0 ? (
            <ul className="space-y-1.5 text-sm leading-relaxed" aria-label="Frases do resumo, cada uma com a fonte">
              {(ins.resumo.frases ?? []).map((f) => (
                <li key={f.chave || f.texto} className="flex flex-wrap items-baseline gap-1.5">
                  {f.nova && (
                    <Badge variant="info" className="text-[11px]">
                      Nova
                    </Badge>
                  )}
                  <span className="flex-1">
                    {r(f.texto)}
                    {f.origem === "ia" && <> <OriginBadge origem="ia" /></>} <SourceRefs patientId={pid} refs={f.refs} />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm leading-relaxed">{r(ins.resumo.texto)}</p>
          )}
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
                <li
                  key={h.hipotese + h.cid}
                  className={cn("rounded-md border p-2.5 text-sm", h.origem === "ia" && "border-dashed", h.decisao === "descartada" && "opacity-60")}
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="font-bold">{r(h.hipotese)}</p>
                    <Badge variant="outline" className="font-mono text-[11px]" title={h.cid_descricao || undefined}>
                      CID {h.cid}
                    </Badge>
                    <Badge variant={CONFIDENCE[h.confianca]} className="text-[11px]">
                      confiança {h.confianca}
                    </Badge>
                    <OriginBadge origem={h.origem} />
                    <NewBadge item={h} falas={ins.falas_analisadas} />
                    {h.decisao && h.decisao !== "pendente" && (
                      <Badge variant={h.decisao === "descartada" ? "outline" : "ok"} className="text-[11px]">
                        {DECISION_LABEL[h.decisao]}
                      </Badge>
                    )}
                  </div>
                  <ItemActions item={h} label={h.hipotese} actions={["aceitar"]} onAction={onInsightAction} />
                  {onHypothesisDecision && (
                    <div className="mt-1 flex flex-wrap gap-1" role="group" aria-label={`Decisão sobre ${h.hipotese}`}>
                      {(["confirmada", "provisoria", "descartada"] as const).map((d) => (
                        <Button
                          key={d}
                          variant={h.decisao === d ? "default" : "outline"}
                          size="sm"
                          className="min-h-9 px-2 text-xs"
                          aria-pressed={h.decisao === d}
                          onClick={() => onHypothesisDecision(h.cid, h.decisao === d ? "pendente" : d)}
                        >
                          {d === "confirmada" ? "Confirmar" : d === "provisoria" ? "Provisória" : "Descartar"}
                        </Button>
                      ))}
                    </div>
                  )}
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
                  {h.cid_descricao && (
                    <p className="mt-1 text-xs text-muted-foreground">CID-10 (DATASUS): {h.cid_descricao}</p>
                  )}
                  <p className="mt-1">
                    <span className="font-bold">Próximos passos: </span>
                    {r(h.proximos_passos)}
                  </p>
                  {h.pergunta_discriminante && (
                    <p className="mt-0.5">
                      <span className="font-bold">Pergunta que separa: </span>
                      {r(h.pergunta_discriminante)}
                    </p>
                  )}
                  {h.exame_discriminante && (
                    <p className="mt-0.5">
                      <span className="font-bold">Exame que separa: </span>
                      {r(h.exame_discriminante)}
                    </p>
                  )}
                  {h.fonte && <p className="mt-0.5 text-xs text-muted-foreground">Fonte: {h.fonte}</p>}
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
              {ins.cirurgia.filter((c) => !(ins.rcri && c.texto === ins.rcri.interpretacao)).map((c) => (
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
          {ins.rcri && <RcriBlock rcri={ins.rcri} pid={pid} r={r} />}
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
                    {m.fonte && <span className="block text-xs text-muted-foreground">Fonte da regra: {m.fonte}</span>}
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
                      <Badge
                        variant={
                          m.status === "em_uso"
                            ? "ok"
                            : m.status === "relatado" || m.status === "planejado" || m.status === "proposto"
                              ? "atencao"
                              : "outline"
                        }
                        className="text-[11px]"
                      >
                        {
                          {
                            em_uso: "Em uso",
                            suspenso: "Suspenso",
                            relatado: "Relatado",
                            planejado: "A prescrever",
                            proposto: "Proposto (rascunho)",
                          }[m.status]
                        }
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
                    {a.criticidade && a.criticidade !== "indeterminada" && (
                      <Badge variant={a.criticidade === "alta" ? "critico" : "outline"} className="text-[11px]">
                        criticidade {a.criticidade}
                      </Badge>
                    )}
                    {a.origem === "ia" && <Badge variant="atencao" className="text-[11px]">IA — confirmar</Badge>}
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
                      <dd className="inline">
                        {a.reatividade_cruzada}
                        {a.fonte_regra && <span className="text-xs text-muted-foreground"> (fonte: {a.fonte_regra})</span>}
                      </dd>
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
                {h.conciliacao && <ConciliationBadge value={h.conciliacao} />}
                <span className="flex-1">
                  {r(h.texto)}
                  {h.cid && <span className="ml-1 font-mono text-xs text-muted-foreground">CID {h.cid}</span>}
                  {h.intercorrencia && <span className="ml-1 text-xs text-atencao">Intercorrência: {h.intercorrencia}</span>}
                  {h.conciliacao === "diverge" && h.observacao && <span className="block text-xs text-muted-foreground">{h.observacao}</span>}
                </span>
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
                    {e.origem_dado && e.origem_dado !== "prontuário" && (
                      <Badge variant="atencao" className="text-[11px]">
                        {e.origem_dado}
                      </Badge>
                    )}
                    {e.conciliacao && e.origem_dado !== "prontuário" && <ConciliationBadge value={e.conciliacao} exam />}
                    {e.tendencia && e.origem_dado === "prontuário" && (
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
                  {e.comparacao && e.origem_dado !== "prontuário" && <p className="mt-0.5 text-xs text-muted-foreground">Comparação: {e.comparacao}</p>}
                  <p className="mt-0.5">{e.explicacao}</p>
                  <SourceRefs patientId={pid} refs={e.refs} />
                </li>
              ))}
            </ul>
          )}
        </AppCard>
      )}

      {show("protocolos") && <ProtocolsCard items={ins.protocolos} patientId={pid} />}

      {show("pedidos") && orders && (
        <AppCard
          title="Pedidos e prescrições em rascunho"
          Icon={ClipboardList}
          count={(ins.pedidos ?? []).length}
          hint="Tirados da fala do profissional e checados contra alergias, função renal, interações e exames recentes antes de aparecer. Nada vai ao prontuário sem a sua assinatura."
        >
          <OrdersCard consultId={orders.consultId} orders={ins.pedidos ?? []} editable onInsights={orders.onInsights} />
        </AppCard>
      )}

      {generic
        .filter((a) => a.aba === tab && !hidden.has(a.id))
        .map((a) => (
          <GenericAppCard key={a.id} result={results.get(a.id) as AppResult} pid={pid} r={r} />
        ))}
    </section>
  );
}

const SEVERITY_BADGE = { critico: "critico", atencao: "atencao", info: "info", ok: "ok" } as const;
const SEVERITY_LABEL = { critico: "Crítico", atencao: "Atenção", info: "Informação", ok: "Em ordem" } as const;

/** Card genérico de um Smart App do registro: lista, tags, tabela ou linha do tempo, conforme o manifest. */
function GenericAppCard({ result, pid, r }: { result: AppResult; pid: string; r: (t: string) => string }) {
  const hint = `Regras sobre o prontuário · v${result.versao}${result.base_versao ? ` · base v${result.base_versao}` : ""}`;
  const itens = result.itens;
  return (
    <AppCard title={result.nome} Icon={ListChecks} hint={hint} count={itens.length}>
      {result.erro ? (
        <p role="alert" className="text-sm text-critico">
          {result.erro}
        </p>
      ) : itens.length === 0 ? (
        <Empty>{result.vazio || "Nada a apontar por enquanto."}</Empty>
      ) : result.card === "tabela" ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">{result.nome}</caption>
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                {result.colunas.map((c) => (
                  <th key={c} scope="col" className="px-2 py-1.5 font-bold">
                    {c}
                  </th>
                ))}
                <th scope="col" className="px-2 py-1.5 font-bold">
                  Fonte
                </th>
              </tr>
            </thead>
            <tbody>
              {itens.map((i, n) => (
                <tr key={`${i.texto}-${n}`} className="border-b last:border-0 align-top">
                  {i.celulas.map((c, k) => (
                    <td key={k} className="px-2 py-1.5">
                      {result.colunas[k] === "Situação" ? (
                        <Badge variant={SEVERITY_BADGE[i.severidade]} className="text-[11px]">
                          <span className="sr-only">{SEVERITY_LABEL[i.severidade]}: </span>
                          {r(c)}
                        </Badge>
                      ) : (
                        r(c)
                      )}
                    </td>
                  ))}
                  <td className="px-2 py-1.5">
                    <SourceRefs patientId={pid} refs={i.refs} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : result.card === "tags" ? (
        <ul className="flex flex-wrap gap-1.5">
          {itens.map((i, n) => (
            <li key={`${i.texto}-${n}`}>
              <Badge variant={SEVERITY_BADGE[i.severidade]} className="min-h-9 text-sm">
                {r(i.texto)}
              </Badge>
              <SourceRefs patientId={pid} refs={i.refs} />
            </li>
          ))}
        </ul>
      ) : (
        <ul className={cn("space-y-2", result.card === "linha_do_tempo" && "border-l-2 pl-3")}>
          {itens.map((i, n) => (
            <li key={`${i.texto}-${n}`} className="flex gap-2 text-sm">
              {i.severidade !== "ok" && <SeverityIcon severity={i.severidade} className="mt-0.5 size-4 shrink-0" />}
              <span className="flex-1">
                {i.quando && <span className="mr-1.5 text-xs font-bold text-primary">{i.quando}</span>}
                <span className="font-bold">{r(i.texto)}</span> <SourceRefs patientId={pid} refs={i.refs} />
                {i.detalhe && <span className="block text-xs text-muted-foreground">{r(i.detalhe)}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </AppCard>
  );
}

const CONCILIATION: Record<Conciliation, { label: string; examLabel: string; variant: "ok" | "atencao" | "critico" | "outline" }> = {
  prontuario: { label: "No prontuário", examLabel: "Coerente com o prontuário", variant: "ok" },
  relatado_nao_registrado: { label: "Só relatado", examLabel: "Sem série no prontuário", variant: "atencao" },
  diverge: { label: "Diverge do prontuário", examLabel: "Diverge do prontuário", variant: "critico" },
};

/** Selo de conciliação: o que o paciente disse × o que o prontuário registra. */
function ConciliationBadge({ value, exam }: { value: Conciliation; exam?: boolean }) {
  const c = CONCILIATION[value];
  return (
    <Badge variant={c.variant} className="text-[11px]">
      {exam ? c.examLabel : c.label}
    </Badge>
  );
}

/** Alertas: os críticos em destaque; os de atenção recolhidos logo abaixo. */
function AlertsCard({
  ins,
  pid,
  r,
  onAction,
}: {
  ins: ConsultInsights;
  pid: string;
  r: (t: string) => string;
  onAction?: OnInsightAction;
}) {
  const critical = ins.alertas.filter((a) => a.severidade === "critico");
  const attention = ins.alertas.filter((a) => a.severidade === "atencao");
  const source = (texto: string) => ins.sugestoes_medicamentos.find((m) => m.texto === texto)?.fonte;
  const fresh = critical.filter((a) => isNewItem(a, ins.falas_analisadas));
  return (
    <AppCard title="Alertas de segurança" Icon={ShieldAlert} hint="Regras determinísticas: rodam sempre, antes da IA." count={critical.length + attention.length}>
      <p role="status" aria-live="polite" className={cn("text-xs font-bold text-critico", fresh.length === 0 && "sr-only")}>
        {fresh.length > 0 ? `Novo alerta crítico: ${r(fresh[0].texto).split(":")[0]}` : ""}
      </p>
      {critical.length > 0 && (
        <ul className="space-y-2">
          {critical.map((m) => (
            <li key={m.texto} className="flex gap-2 rounded-md border border-critico-border bg-critico-bg p-2 text-sm">
              <SeverityIcon severity="critico" className="mt-0.5 size-5 shrink-0" />
              <div>
                <p className="font-bold text-critico">
                  {m.fixado && <Pin aria-label="Fixado no topo" className="mr-1 inline size-4" />}
                  {r(m.texto)} <NewBadge item={m} falas={ins.falas_analisadas} />
                </p>
                {source(m.texto) && <p className="text-xs text-muted-foreground">Fonte da regra: {source(m.texto)}</p>}
                <SourceRefs patientId={pid} refs={m.refs} />
                <ItemActions item={m} label={m.texto} actions={["aceitar", "nao_se_aplica", "descartar"]} onAction={onAction} />
              </div>
            </li>
          ))}
        </ul>
      )}
      {attention.length > 0 && (
        <details className={cn("rounded-md border", critical.length > 0 && "mt-2")}>
          <summary className="min-h-11 cursor-pointer px-3 py-2 text-sm font-bold">
            {attention.length} alerta{attention.length > 1 ? "s" : ""} de atenção
          </summary>
          <ul className="space-y-1.5 border-t p-3">
            {attention.map((m) => (
              <li key={m.texto} className="flex gap-2 text-sm">
                <SeverityIcon severity="atencao" className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">
                  {r(m.texto)} <NewBadge item={m} falas={ins.falas_analisadas} /> <SourceRefs patientId={pid} refs={m.refs} />
                  {source(m.texto) && <span className="block text-xs text-muted-foreground">Fonte da regra: {source(m.texto)}</span>}
                  <ItemActions item={m} label={m.texto} actions={["aceitar", "nao_se_aplica", "descartar"]} onAction={onAction} />
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </AppCard>
  );
}

/** Índice de Lee (RCRI) da cirurgia planejada: pontos por critério, cada um com o recurso que o sustenta. */
function RcriBlock({ rcri, pid, r }: { rcri: Rcri; pid: string; r: (t: string) => string }) {
  return (
    <div className="mt-3 space-y-2 border-t pt-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <h4 className="text-sm font-bold">Risco cardíaco perioperatório (RCRI)</h4>
        <Badge variant={rcri.pontos === 0 ? "ok" : rcri.pontos === 1 ? "atencao" : "critico"} className="text-[11px]">
          {rcri.pontos} ponto{rcri.pontos === 1 ? "" : "s"} · classe {rcri.classe}
        </Badge>
      </div>
      <p className="text-sm">{r(rcri.interpretacao)}</p>
      <ul className="space-y-1 text-sm" aria-label={`Critérios do RCRI para ${rcri.cirurgia}`}>
        {rcri.itens.map((i) => (
          <li key={i.criterio} className="flex flex-wrap items-baseline gap-1.5">
            <Badge variant={i.presente ? "atencao" : "outline"} className="text-[11px]">
              {i.pontos} {i.pontos === 1 ? "ponto" : "pontos"}
            </Badge>
            <span className="flex-1">
              <span className="font-bold">{i.criterio}.</span> {r(i.evidencia)} <SourceRefs patientId={pid} refs={i.refs} />
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">Fonte: {rcri.fonte}</p>
    </div>
  );
}

const PROTOCOL_STATUS: Record<ProtocolItem["status"], { label: string; variant: "ok" | "atencao" | "critico" | "outline" }> = {
  em_dia: { label: "Em dia", variant: "ok" },
  no_alvo: { label: "No alvo", variant: "ok" },
  pendente: { label: "Pendente", variant: "atencao" },
  sem_registro: { label: "Sem registro", variant: "atencao" },
  fora_do_alvo: { label: "Fora do alvo", variant: "critico" },
  abordado: { label: "Abordado", variant: "ok" },
  critico: { label: "Risco crítico", variant: "critico" },
};

function ProtocolsCard({ items, patientId }: { items: ProtocolItem[]; patientId: string }) {
  const axes = [...new Set(items.map((i) => i.eixo || "Protocolos"))];
  return (
    <AppCard
      title="Protocolos de acompanhamento"
      Icon={ListChecks}
      hint="Linhas de cuidado da atenção primária por eixo, com a fonte oficial. O que for dito na consulta aparece como abordado."
    >
      {items.length === 0 ? (
        <Empty>Nenhum protocolo se aplica a este paciente.</Empty>
      ) : (
        <div className="space-y-4">
          {axes.map((axis) => {
            const inAxis = items.filter((i) => (i.eixo || "Protocolos") === axis);
            const groups = [...new Set(inAxis.map((i) => i.protocolo))];
            return (
              <section key={axis} aria-label={`Eixo ${axis}`}>
                <h4 className="text-xs font-bold uppercase tracking-wide text-muted-foreground">{axis}</h4>
                <div className="mt-1 space-y-3">
                  {groups.map((g) => (
                    <div key={g}>
                      <p className="flex items-center gap-1.5 text-sm font-bold">
                        <FileHeart aria-hidden className="size-4 text-primary" /> {g}
                      </p>
                      <ul className="mt-1 space-y-1.5">
                        {inAxis
                          .filter((i) => i.protocolo === g)
                          .map((i) => (
                            <li key={i.id || i.item} className="text-sm">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <Badge variant={PROTOCOL_STATUS[i.status].variant} className="text-[11px]">
                                  {PROTOCOL_STATUS[i.status].label}
                                </Badge>
                                <span className="font-bold">{i.item}</span>
                                {i.local && (
                                  <Badge variant="outline" className="text-[11px]">
                                    Local
                                  </Badge>
                                )}
                                <span className="text-xs text-muted-foreground">{i.ultimo ?? i.orientacao}</span>
                                <SourceRefs patientId={patientId} refs={i.refs} />
                              </div>
                              {i.fonte && <p className="text-xs text-muted-foreground">Fonte: {i.fonte}</p>}
                            </li>
                          ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </AppCard>
  );
}

