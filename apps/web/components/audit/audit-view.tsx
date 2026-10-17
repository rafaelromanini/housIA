"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { FileClock, Filter, OctagonAlert, RefreshCw, ShieldOff } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { AuditEntry, AuditMetrics, BedRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dayOf, fmtDateTime, ROLE_LABEL } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

const ACTION_LABEL: Record<string, string> = {
  "auth.login": "Login",
  "handoff.gerado": "Passagem de plantão gerada",
  "handoff.cache_hit": "Passagem servida do cache",
  "handoff.visualizado": "Passagem visualizada",
  "handoff.refs_invalidas": "Itens da IA descartados (sem fonte)",
  "handoff.erro_llm": "Erro do LLM na passagem",
  "medrec.executada": "Reconciliação executada",
  "medrec.visualizada": "Reconciliação visualizada",
  "medrec.erro_llm": "Erro do LLM na reconciliação",
  "discharge.avaliacao": "Avaliação de alta",
  "discharge.override": "Override de alta (justificado)",
  "discharge.confirmacao_bloqueada": "Confirmação de alta bloqueada (trava)",
  "discharge.confirmada": "Alta confirmada",
  "discharge.erro_llm": "Erro do LLM na alta",
  "alerta.reconhecido": "Alerta reconhecido",
  "alerta.descartado": "Alerta descartado",
  "alerta.aberto": "Alerta reaberto",
  "alerta.reaberto": "Alerta reaberto (severidade subiu)",
  "handoff.conferencia": "Conferência da passagem verbal",
  "discharge.documentos": "Documentos de alta gerados",
  "ia.feedback": "Avaliação da IA (útil/não útil)",
  "jornada.tarefa_criada": "Tarefa da jornada criada",
  "jornada.tarefa_concluida": "Tarefa da jornada concluída",
  "jornada.tarefa_cancelada": "Tarefa da jornada cancelada",
  "jornada.contato_registrado": "Contato pós-alta registrado",
  "consulta.iniciada": "Consulta iniciada",
  "consulta.visualizada": "Consulta visualizada",
  "consulta.consentimento": "Consentimento do paciente registrado (por finalidade)",
  "consulta.marco_privacidade": "Marco de privacidade (pausa, retomada ou revogação)",
  "consulta.fala_editada": "Fala da transcrição corrigida (o texto não vai para o registro)",
  "consulta.audio": "Áudio transcrito (o áudio não é guardado)",
  "consulta.analise_ia": "Análise da consulta pela IA",
  "consulta.traducao": "Falas traduzidas",
  "consulta.fala_incluida_na_nota": "Fala incluída na nota",
  "consulta.erro_llm": "Erro do LLM na consulta",
  "consulta.queixa_principal": "Queixa principal fixada",
  "consulta.hipotese": "Hipótese decidida (confirmada, provisória ou descartada)",
  "consulta.insight_acao": "Insight da consulta decidido (aceito, descartado com motivo, feito ou fixado)",
  "consulta.cirurgia_segura": "Lista de cirurgia segura marcada",
  "consulta.opme_guia": "Guia de OPME conferida",
  "consulta.opme_consumo": "Consumo de OPME lido (etiqueta)",
  "consulta.opme_consumo_removido": "Consumo de OPME removido",
  "consulta.faturamento": "Convênio e guia do faturamento",
  "consulta.pedido_assinado": "Pedido ou prescrição assinado",
  "consulta.pedido_recusado": "Pedido ou prescrição recusado",
  "consulta.pedido_revertido": "Pedido ou prescrição voltou a pendente",
  "consulta.copiloto": "Pergunta ao copiloto",
  "consulta.copiloto_inserido": "Resposta do copiloto inserida na nota",
  "consulta.finalizada": "Consulta finalizada (nota gerada)",
  "consulta.nota_revisada": "Nota revisada pelo profissional",
  "consulta.exportada": "Nota exportada (envio automático desligado)",
  "consulta.enviada": "Nota enviada ao prontuário",
  "cirurgia.prontidao": "Prontidão para faturamento verificada",
  "impacto.linha_de_base": "Linha de base de tempo informada (Impacto)",
  "gestao.resumo_semanal": "Resumo semanal da gestão gerado",
  "gestao.erro_llm": "Erro do LLM no resumo da gestão",
};

const FILTERS = [
  { value: "", label: "Todas as ações" },
  { value: "handoff.", label: "Passagem de plantão" },
  { value: "medrec.", label: "Reconciliação" },
  { value: "discharge.", label: "Alta" },
  { value: "discharge.override", label: "Overrides de alta" },
  { value: "alerta.", label: "Mudanças de status de alerta" },
  { value: "jornada.", label: "Jornada do paciente" },
  { value: "consulta.", label: "Consulta com IA" },
  { value: "consulta.nota_revisada", label: "Consulta: notas revisadas" },
  { value: "consulta.enviada", label: "Consulta: notas enviadas" },
  { value: "cirurgia.", label: "Centro cirúrgico (prontidão)" },
  { value: "impacto.", label: "Painel Impacto" },
  { value: "gestao.", label: "Painel de gestão" },
  { value: "auth.", label: "Logins" },
];

const isoDay = (d: Date) => dayOf(d.toISOString()); // data local (America/Sao_Paulo)

function StatTile({ label, value, hint, tone }: { label: string; value: number; hint?: string; tone?: "critico" }) {
  return (
    <div className={cn("rounded-lg border bg-card p-3 shadow-sm", tone === "critico" && value > 0 && "border-critico-border")}>
      <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
        {tone === "critico" && value > 0 && <OctagonAlert aria-label="Crítico" className="size-4 text-critico" />}
        {label}
      </p>
      <p className="mt-1 text-3xl font-bold leading-none">{value.toLocaleString("pt-BR")}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function PercentTile({ label, value, hint }: { label: string; value: number | null; hint?: string }) {
  return (
    <div className="rounded-lg border bg-card p-3 shadow-sm">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-1 text-3xl font-bold leading-none">{value == null ? "—" : `${Math.round(value * 100)}%`}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function summarize(e: AuditEntry): string {
  const p = e.payload ?? {};
  switch (e.action) {
    case "discharge.override":
      return `“${p.justificativa}” (LACE ${p.lace}, social ${p.social})`;
    case "discharge.avaliacao":
      return `Risco ${p.risco} · LACE ${p.lace} · social ${p.social} · ${p.sugestoes} sugestão(ões)`;
    case "handoff.gerado":
      return `${p.audience} · ${p.model} · prompt v${p.prompt_version}${p.itens_descartados ? ` · ${p.itens_descartados} descartado(s)` : ""}`;
    case "medrec.executada":
      return `${p.origem} → ${p.destino} · ${p.criticos} crítico(s) · ${p.hipoteses} hipótese(s)`;
    case "handoff.conferencia":
      return `${p.mencionado} citado(s) · ${p.omitido} omitido(s) (${p.omitidos_criticos} crítico) · ${p.divergente} divergência(s)`;
    case "ia.feedback":
      if (e.entity === "nps") return `NPS: nota ${p.nota}${p.comentario ? ` — “${p.comentario}”` : ""}`;
      return `${e.entity}: ${p.util ? "útil" : "não útil"}${p.comentario ? ` — “${p.comentario}”` : ""}`;
    case "alerta.reconhecido":
    case "alerta.descartado":
      return `${p.titulo}${p.justificativa ? ` — “${p.justificativa}”` : ""}`;
    case "jornada.tarefa_criada":
      return `${p.regra} · ${p.papel} · prazo ${p.prazo} · ${p.fhir}`;
    case "jornada.contato_registrado":
      return `${p.canal} · ${p.atendeu ? "atendeu" : "sem sucesso"}${(p.alarmes as string[] | undefined)?.length ? ` · alarme: ${(p.alarmes as string[]).join(", ")}` : ""}`;
    case "consulta.finalizada":
      return `${p.falas} fala(s) · ${p.modo} · ${Math.round(Number(p.duracao_s ?? 0) / 60)} min`;
    case "consulta.consentimento":
      return p.recusou ? "Recusou a IA (consulta só com as regras)" : `Consentiu: ${p.quem}`;
    case "consulta.marco_privacidade":
      return `${p.marco} · fala ${p.n}`;
    case "consulta.fala_editada":
      return `fala ${p.n} · ${(p.campos as string[] | undefined)?.join(" e ") ?? ""}${p.nota_desatualizada ? " · nota desatualizada" : ""}`;
    case "consulta.nota_revisada":
      return `${p.editado_pct ?? 0}% do texto editado (${p.palavras_alteradas ?? 0} palavra(s))`;
    case "consulta.enviada":
    case "consulta.exportada":
      return p.tempo_ate_envio_s == null ? "" : `${Math.round(Number(p.tempo_ate_envio_s) / 60)} min após a finalização`;
    case "cirurgia.prontidao":
      return `${p.momento}: ${(p.antes as { score?: number } | null)?.score ?? "—"} → ${(p.depois as { score?: number }).score} pontos`;
    case "impacto.linha_de_base":
      return `${p.setor}: ${p.minutos_antes} min por atendimento antes do HousIA`;
    case "alerta.reaberto":
      return `${p.titulo ?? ""} — agora ${p.severidade === "critico" ? "crítico" : p.severidade}`;
    default:
      return "";
  }
}

export function AuditView() {
  const { user } = useAuth();
  const today = new Date();
  const [since, setSince] = useState(isoDay(new Date(today.getTime() - 7 * 86400000)));
  const [until, setUntil] = useState(isoDay(today));
  const [action, setAction] = useState("");
  const query = useMemo(() => {
    const p = new URLSearchParams({ since, until, limit: "200" });
    if (action) p.set("action", action);
    return p.toString();
  }, [since, until, action]);

  const allowed = user?.role === "gestor";
  const metrics = useApi<AuditMetrics>(allowed ? `/audit/metrics?since=${since}&until=${until}` : null);
  const log = useApi<{ total: number; items: AuditEntry[] }>(allowed ? `/audit?${query}` : null);
  // nome e leito no lugar do id interno do paciente
  const beds = useApi<BedRow[]>(allowed ? "/patients" : null);
  const patientName = useMemo(() => new Map((beds.data ?? []).map((b) => [b.id, b])), [beds.data]);

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden />
        A auditoria é restrita ao perfil de gestão.
      </Alert>
    );
  }

  const m = metrics.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Auditoria</h1>
          <p className="text-sm text-muted-foreground">Gerações de IA, visualizações, overrides e mudanças de status de alertas.</p>
        </div>
        <Button
          variant="outline"
          onClick={() => {
            void metrics.reload();
            void log.reload();
          }}
        >
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="grid gap-3 sm:grid-cols-[repeat(3,minmax(0,1fr))]">
            <div className="space-y-1.5">
              <Label htmlFor="desde">De</Label>
              <Input id="desde" type="date" value={since} max={until} onChange={(e) => setSince(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ate">Até</Label>
              <Input id="ate" type="date" value={until} min={since} onChange={(e) => setUntil(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="acao">Ação</Label>
              <NativeSelect id="acao" value={action} onChange={(e) => setAction(e.target.value)}>
                {FILTERS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
          </div>
        </CardContent>
      </Card>

      <section aria-label="Métricas do período" className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {!m ? (
          Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-24" />)
        ) : (
          <>
            <StatTile
              label="Alertas críticos"
              value={m.alertas_criticos}
              tone="critico"
              hint={Object.entries(m.alertas_criticos_por_origem)
                .map(([k, v]) => `${{ handoff: "passagem", medrec: "reconciliação", discharge: "alta", consulta: "consulta" }[k] ?? k}: ${v}`)
                .join(" · ") || undefined}
            />
            <StatTile label="Overrides de alta" value={m.overrides} hint="Com justificativa registrada" />
            <StatTile label="Altas de alto risco" value={m.altas_alto_risco} hint={`${m.altas_confirmadas} alta(s) confirmada(s)`} />
            <StatTile label="Passagens geradas" value={m.passagens_geradas} hint={`${m.passagens_do_cache} servida(s) do cache`} />
            <StatTile label="Reconciliações" value={m.reconciliacoes} />
            <StatTile label="Alertas reconhecidos" value={m.alertas_por_status.reconhecido ?? 0} />
            <StatTile label="Alertas em aberto" value={m.alertas_por_status.aberto ?? 0} />
            <StatTile label="Itens da IA sem fonte" value={m.itens_ia_descartados_sem_fonte} hint="Descartados antes de exibir" />
            <PercentTile
              label="Utilidade percebida da IA"
              value={m.taxa_utilidade_ia}
              hint={(() => {
                const f = Object.values(m.feedback_ia);
                const total = f.reduce((n, x) => n + x.util + x.nao_util, 0);
                return total ? `${total} avaliação(ões) "isto foi útil?"` : "Sem avaliações no período";
              })()}
            />
            <PercentTile
              label="Aceitação de alertas"
              value={m.taxa_aceitacao_alertas}
              hint="Reconhecidos ÷ (reconhecidos + descartados)"
            />
          </>
        )}
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileClock aria-hidden className="size-5 text-primary" /> Registro de auditoria
            {log.data && <span className="text-sm font-normal text-muted-foreground">({log.data.total})</span>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {log.error && <Alert variant="critico">{log.error}</Alert>}
          {!log.data && !log.error && <Skeleton className="h-64" />}
          {log.data && log.data.items.length === 0 && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Filter aria-hidden className="size-4" /> Nenhum registro no período/filtro.
            </p>
          )}
          {log.data && log.data.items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <caption className="sr-only">Registros de auditoria, mais recentes primeiro</caption>
                <thead>
                  <tr className="border-b text-left">
                    <th scope="col" className="py-2 pr-3">Quando</th>
                    <th scope="col" className="py-2 pr-3">Usuário</th>
                    <th scope="col" className="py-2 pr-3">Ação</th>
                    <th scope="col" className="py-2 pr-3">Paciente</th>
                    <th scope="col" className="py-2">Detalhes</th>
                  </tr>
                </thead>
                <tbody>
                  {log.data.items.map((e) => {
                    const pid = (e.payload?.patient_id as string | undefined) ?? (e.entity === "patient" ? e.entity_id : null);
                    const highlight = e.action === "discharge.override" || e.action === "discharge.confirmacao_bloqueada";
                    return (
                      <tr key={e.id} className={cn("border-b align-top", highlight && "bg-atencao-bg/60")}>
                        <td className="whitespace-nowrap py-2 pr-3 tabular-nums">{fmtDateTime(e.created_at)}</td>
                        <td className="py-2 pr-3">
                          {e.user ? (
                            <>
                              {e.user.name}
                              <span className="block text-xs text-muted-foreground">{ROLE_LABEL[e.user.role]}</span>
                            </>
                          ) : (
                            <>
                              HousIA
                              <span className="block text-xs text-muted-foreground">automático</span>
                            </>
                          )}
                        </td>
                        <td className="py-2 pr-3">
                          <span className="font-bold">{ACTION_LABEL[e.action] ?? e.action}</span>
                          <span className="block font-mono text-[11px] text-muted-foreground">{e.action}</span>
                        </td>
                        <td className="py-2 pr-3">
                          {pid ? (
                            <Link
                              className="inline-flex min-h-9 flex-col justify-center font-bold text-primary hover:underline"
                              href={`/pacientes/${pid}`}
                            >
                              {patientName.get(pid)?.nome ?? pid}
                              {patientName.get(pid) && (
                                <span className="text-xs font-normal text-muted-foreground">
                                  {patientName.get(pid)!.setor} · leito {patientName.get(pid)!.leito}
                                </span>
                              )}
                            </Link>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="py-2">
                          <p>{summarize(e)}</p>
                          <details>
                            <summary className="cursor-pointer text-xs text-muted-foreground">payload</summary>
                            <pre className="mt-1 max-w-xl overflow-x-auto rounded bg-muted p-2 text-[11px]">
                              {JSON.stringify(e.payload, null, 2)}
                            </pre>
                          </details>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-3 text-xs text-muted-foreground">
            <Badge variant="outline">LGPD</Badge> O registro guarda metadados das gerações (modelo, versão do prompt, contagens), nunca o
            texto clínico completo enviado ao LLM.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
