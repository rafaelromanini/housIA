"use client";

import { useState } from "react";
import { CircleCheck, ClipboardCheck, ImageUp, Loader2, ShieldCheck, TriangleAlert } from "lucide-react";

import { OpmePanel } from "@/components/consult/opme-panel";
import { ReadinessCard } from "@/components/consult/readiness-card";
import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ChecklistStep, ConsultInsights, Consultation, SafetyCheck, SurgicalReport } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const CHECK_STATUS: Record<SafetyCheck["status"], { label: string; variant: "ok" | "outline" | "info" | "atencao" | "critico" }> = {
  ok: { label: "OK", variant: "ok" },
  pendente: { label: "Pendente", variant: "outline" },
  info: { label: "Informação", variant: "info" },
  atencao: { label: "Atenção", variant: "atencao" },
  critico: { label: "Crítico", variant: "critico" },
};

const ORIGIN_LABEL = {
  ditado: "escolhido pelo procedimento ditado",
  agendamento: "escolhido pelo procedimento agendado",
  especialidade: "pela especialidade selecionada",
  base: "modelo base",
} as const;

const GROUPS: { key: SafetyCheck["grupo"]; label: string }[] = [
  { key: "sitio", label: "Sítio e lateralidade" },
  { key: "contagem", label: "Contagem" },
  { key: "intraoperatorio", label: "Intraoperatório" },
  { key: "profilaxia", label: "Antibioticoprofilaxia" },
];

const WHO_STEPS: ChecklistStep[] = ["sign_in", "time_out", "sign_out"];
const WHO_SHORT: Record<ChecklistStep, string> = { sign_in: "sign-in", time_out: "time-out", sign_out: "sign-out" };

function CheckRow({ item, patientId }: { item: SafetyCheck; patientId: string }) {
  const st = CHECK_STATUS[item.status];
  return (
    <li className="space-y-0.5 border-b py-2 text-sm last:border-b-0">
      <p className="flex flex-wrap items-center gap-2">
        <Badge variant={st.variant} className="text-[11px]">
          {st.label}
        </Badge>
        <span className="font-bold">{item.rotulo}</span>
      </p>
      <p>
        {item.texto} <SourceRefs patientId={patientId} refs={item.refs} />
      </p>
      <p className="text-xs text-muted-foreground">Fonte: {item.fonte}</p>
    </li>
  );
}

/** Centro cirúrgico: o relatório se monta enquanto a equipe dita; modelo por procedimento, Cirurgia Segura, pendências e OPME. */
export function SurgicalPanel({
  report,
  patientId,
  onChecklist,
  consultation,
  onInsights,
}: {
  report: SurgicalReport;
  patientId: string;
  /** Marca uma etapa da Cirurgia Segura por botão (só durante o ditado). */
  onChecklist?: (step: ChecklistStep, done: boolean) => Promise<void>;
  /** Com a consulta em andamento e `onInsights`, a guia de OPME, as etiquetas e o convênio podem ser editados. */
  consultation?: Consultation;
  onInsights?: (insights: ConsultInsights | null, patch?: Partial<Consultation>) => void;
}) {
  const [marking, setMarking] = useState<ChecklistStep | null>(null);
  const filled = report.campos.filter((f) => f.texto).length;
  const checks = report.verificacoes ?? [];
  const who = checks.filter((c) => c.grupo === "cirurgia_segura");
  const pedidos = report.pedidos ?? [];
  const peri = report.perioperatorio ?? [];
  const modelo = report.modelo;

  async function mark(step: ChecklistStep, done: boolean) {
    if (!onChecklist) return;
    setMarking(step);
    try {
      await onChecklist(step, done);
    } finally {
      setMarking(null);
    }
  }

  return (
    <section aria-label="Relatório cirúrgico" className="space-y-3">
      {report.prontidao && (
        <ReadinessCard
          readiness={report.prontidao}
          patientId={patientId}
          consultId={consultation?.id}
          numeroGuia={consultation?.faturamento?.numero_guia ?? consultation?.opme_guia?.numero}
          onInsights={onInsights}
        />
      )}
      <Card className={cn(report.pendencias.some((p) => p.severidade === "critico") && "border-critico-border")}>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <TriangleAlert aria-hidden className="size-5 text-primary" /> Verificações automáticas
          </CardTitle>
          <CardDescription>
            Campos obrigatórios, materiais, alergias, Cirurgia Segura, lateralidade, contagem e profilaxia — o que costuma gerar glosa,
            devolução ou evento adverso.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {report.pendencias.length === 0 ? (
            <p className="inline-flex items-center gap-1 text-sm font-bold text-ok">
              <CircleCheck aria-hidden className="size-4" /> Sem pendências.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {report.pendencias.map((p, i) => (
                <li key={`${i}-${p.texto}`} className="flex gap-2 text-sm">
                  <SeverityIcon severity={p.severidade} className="mt-0.5 size-4 shrink-0" />
                  <span className="flex-1">
                    {p.texto} <SourceRefs patientId={patientId} refs={p.refs} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {checks.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck aria-hidden className="size-5 text-primary" /> Cirurgia segura e checagens perioperatórias
            </CardTitle>
            <CardDescription>
              Cada checagem mostra a regra e a fonte. As etapas da OMS valem por voz (“time-out realizado”) ou pelo botão.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <h3 className="mb-1 text-sm font-bold">Lista de verificação da OMS</h3>
              <ul className="grid gap-2 sm:grid-cols-3">
                {WHO_STEPS.map((step) => {
                  const item = who.find((c) => c.chave === step);
                  if (!item) return null;
                  const st = CHECK_STATUS[item.status];
                  const done = item.status === "ok";
                  const byVoice = done && !item.marcado_por;
                  return (
                    <li key={step} className={cn("rounded-md border p-2 text-sm", item.status === "critico" && "border-critico-border")}>
                      <p className="font-bold">{item.rotulo}</p>
                      <Badge variant={st.variant} className="mt-1 text-[11px]">
                        {st.label}
                      </Badge>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.texto}
                        {item.marcado_por && ` Por ${item.marcado_por}${item.em ? ` em ${fmtDateTime(item.em)}` : ""}.`}
                      </p>
                      <SourceRefs patientId={patientId} refs={item.refs} />
                      {onChecklist && (
                        <Button
                          variant={done ? "outline" : "default"}
                          size="sm"
                          className="mt-1 w-full"
                          aria-pressed={done}
                          aria-label={done ? `Desmarcar ${WHO_SHORT[step]}` : `Marcar ${WHO_SHORT[step]} como realizado`}
                          disabled={marking !== null || byVoice}
                          onClick={() => void mark(step, !done)}
                        >
                          {marking === step ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />}
                          {byVoice ? "Feito por voz" : done ? "Desmarcar" : `Marcar ${WHO_SHORT[step]}`}
                        </Button>
                      )}
                    </li>
                  );
                })}
              </ul>
              <ul className="mt-2">
                {who
                  .filter((c) => c.chave === "consentimento")
                  .map((c) => (
                    <CheckRow key={c.chave} item={c} patientId={patientId} />
                  ))}
              </ul>
            </div>
            {GROUPS.map(({ key, label }) => {
              const items = checks.filter((c) => c.grupo === key);
              if (items.length === 0) return null;
              return (
                <div key={key}>
                  <h3 className="text-sm font-bold">{label}</h3>
                  <ul>
                    {items.map((c) => (
                      <CheckRow key={c.chave} item={c} patientId={patientId} />
                    ))}
                  </ul>
                </div>
              );
            })}
            {peri.length > 0 && (
              <div>
                <h3 className="text-sm font-bold">Do prontuário (pré-operatório)</h3>
                <ul className="mt-1 space-y-1">
                  {peri.map((p) => (
                    <li key={p.texto} className="text-sm">
                      {p.texto} <SourceRefs patientId={patientId} refs={p.refs} />
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <ClipboardCheck aria-hidden className="size-5 text-primary" /> Relatório estruturado
            <span className="text-sm font-normal text-muted-foreground">
              ({filled}/{report.campos.length} campos)
            </span>
          </CardTitle>
          <CardDescription>
            {modelo ? (
              <>
                Modelo: <span className="font-bold text-foreground">{modelo.rotulo}</span> · {modelo.especialidade_rotulo} · v{modelo.versao} —{" "}
                {ORIGIN_LABEL[modelo.origem]} <SourceRefs patientId={patientId} refs={modelo.refs} />
              </>
            ) : (
              "Cada campo vem do ditado e mostra a fala de origem."
            )}
          </CardDescription>
          {report.agendado && (
            <p className="text-xs text-muted-foreground">
              Agendado: {report.agendado.procedimento}
              {report.agendado.sitio && ` — ${report.agendado.sitio}`}
            </p>
          )}
        </CardHeader>
        <CardContent>
          <dl className="divide-y">
            {report.campos.map((f) => (
              <div key={f.campo} className="grid gap-1 py-2 text-sm sm:grid-cols-[12rem_1fr]">
                <dt className="font-bold">
                  {f.rotulo}
                  {f.obrigatorio && <span className="text-critico"> *</span>}
                </dt>
                <dd>
                  {f.texto ? (
                    <>
                      {f.texto} <SourceRefs patientId={patientId} refs={f.refs} />
                    </>
                  ) : (
                    <>
                      <span className={cn("italic", f.obrigatorio ? "text-atencao" : "text-muted-foreground")}>
                        {f.obrigatorio ? "Não ditado — obrigatório" : "Não ditado"}
                      </span>
                      {f.obrigatorio && f.motivo && (
                        <span className="block text-xs text-muted-foreground">
                          Por quê: {f.motivo}
                          {f.fonte && ` (${f.fonte})`}
                        </span>
                      )}
                    </>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {report.cids.length > 0 && (
            <p className="mt-2 flex flex-wrap items-center gap-1.5 text-sm">
              <span className="font-bold">CID-10:</span>
              {report.cids.map((c) => (
                <Badge key={c.codigo} variant="outline" className="font-mono">
                  {c.codigo} {c.descricao}
                </Badge>
              ))}
            </p>
          )}
        </CardContent>
      </Card>

      {pedidos.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <ImageUp aria-hidden className="size-5 text-primary" /> Pedidos propostos (RIS)
            </CardTitle>
            <CardDescription>
              Vão ao prontuário como proposta (ServiceRequest) junto com o relatório aprovado e aparecem como exame pendente na passagem de
              plantão e no Waze da alta.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1.5">
              {pedidos.map((p) => (
                <li key={p.descricao} className="text-sm">
                  <span className="font-bold">{p.descricao}</span>
                  <span className="block text-muted-foreground">
                    {p.motivo} <SourceRefs patientId={patientId} refs={p.refs} />
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <OpmePanel
        report={report}
        patientId={patientId}
        consultId={consultation?.id}
        guide={consultation?.opme_guia}
        onInsights={onInsights}
      />
    </section>
  );
}
