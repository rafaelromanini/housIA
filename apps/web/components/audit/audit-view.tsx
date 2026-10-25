"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Download, FileClock, Filter, OctagonAlert, RefreshCw, ShieldCheck, ShieldOff } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { AuditEntry, AuditMetrics, BedRow } from "@/lib/api";
import { api, downloadFile, type ChainStatus } from "@/lib/api";
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
  "consulta.fala_confirmada": "Fala de baixa confiança conferida pelo profissional (o texto não vai para o registro)",
  "consulta.vocabulario": "Vocabulário institucional da transcrição atualizado",
  "consulta.voz_cadastrada": "Perfil de voz do profissional cadastrado (só o vetor, cifrado; o áudio não é guardado)",
  "consulta.voz_apagada": "Perfil de voz do profissional apagado",
  "consulta.audio_stream": "Áudio em fluxo transcrito no servidor (o áudio não é guardado)",
  "consulta.analise_ia": "Análise da consulta pela IA",
  "consulta.traducao": "Falas traduzidas",
  "consulta.fala_incluida_na_nota": "Fala incluída na nota",
  "consulta.erro_llm": "Erro do LLM na consulta",
  "llm.chamada": "Chamada ao provedor de IA (provedor, modelo, região e dados pseudonimizados por camada)",
  "consulta.queixa_principal": "Queixa principal fixada",
  "consulta.hipotese": "Hipótese decidida (confirmada, provisória ou descartada)",
  "consulta.insight_acao": "Insight da consulta decidido (aceito, descartado com motivo, feito ou fixado)",
  "consulta.cirurgia_segura": "Lista de cirurgia segura marcada",
  "consulta.escala": "Respostas de escala clínica registradas (IPSS, PHQ-9, GAD-7, Morse, Braden, IVCF-20, NYHA, IMC)",
  "consulta.opme_guia": "Guia de OPME conferida",
  "consulta.opme_consumo": "Consumo de OPME lido (etiqueta)",
  "consulta.opme_consumo_removido": "Consumo de OPME removido",
  "consulta.faturamento": "Convênio e guia do faturamento",
  "consulta.pedido_assinado": "Pedido ou prescrição assinado",
  "consulta.pedido_recusado": "Pedido ou prescrição recusado",
  "consulta.pedido_revertido": "Pedido ou prescrição voltou a pendente",
  "consulta.encaminhamento": "Escolha no card Antes de encaminhar",
  "consulta.autorizacao_operadora": "Operadora da autorização prévia definida na consulta",
  "consulta.autorizacao_gerada": "Pacote de justificativa da autorização prévia gerado (rascunho)",
  "consulta.autorizacao_editada": "Justificativa da autorização prévia editada pelo profissional",
  "consulta.autorizacao_aprovada": "Pacote da autorização prévia revisado e aprovado pelo médico",
  "consulta.autorizacao_bloqueada": "Aprovação do pacote barrada: afirmação sem evidência válida",
  "consulta.autorizacao_status": "Andamento da autorização prévia (rascunho, enviado, aprovado, negado)",
  "consulta.autorizacao_exportada": "Pacote da autorização prévia exportado (PDF ou JSON)",
  "consulta.copiloto": "Pergunta ao copiloto",
  "consulta.copiloto_inserido": "Resposta do copiloto inserida na nota",
  "consulta.finalizada": "Consulta finalizada (nota gerada)",
  "consulta.nota_revisada": "Nota revisada pelo profissional",
  "consulta.modelo_nota": "Modelo da nota trocado na revisão",
  "consulta.exportada": "Nota exportada (envio automático desligado)",
  "consulta.exportada_pdf": "Nota baixada em PDF",
  "consulta.ledi_exportado": "Atendimento exportado para o e-SUS APS (LEDI)",
  "org.cadastro_br": "Cadastro brasileiro atualizado (CNES, CNS ou CBO)",
  "paciente.avulso_criado": "Paciente avulso cadastrado",
  "consulta.envio_com_pendencia": "Nota enviada com pendência crítica justificada",
  "consulta.enviada": "Nota enviada ao prontuário",
  "consulta.envio_erro": "Envio ao prontuário falhou (a nota ficou na fila para reenvio)",
  "consulta.expurgada": "Consulta expurgada pelo prazo de retenção",
  "consulta.dados_excluidos": "Dados da consulta excluídos a pedido do titular",
  "consulta.propostas": "Propostas para o prontuário aceitas pelo profissional",
  "consulta.adendo_bloqueado": "Adendo bloqueado por regra de segurança",
  "consulta.adendo_ditado": "Adendo ditado após a finalização",
  "consulta.documento_aprovado": "Documento clínico aprovado",
  "consulta.documento_editado": "Documento clínico editado",
  "consulta.documento_enviado": "Documento clínico enviado ao prontuário",
  "consulta.documento_exportado": "Documento clínico exportado",
  "consulta.documento_gerado": "Documento clínico gerado",
  "consulta.estilo_alterado": "Estilo de escrita da nota alterado",
  "consulta.estilo_apagado": "Estilo de escrita da nota apagado",
  "consulta.laudo_enviado": "Laudo de exame recebido",
  "consulta.laudo_recusado": "Laudo de exame recusado",
  "consulta.laudo_removido": "Laudo de exame removido",
  "consulta.nota_reescrita": "Nota reescrita no estilo do profissional",
  "consulta.nota_reescrita_bloqueada": "Reescrita da nota bloqueada por regra de segurança",
  "consulta.nota_reescrita_previa": "Prévia da reescrita da nota gerada",
  "consulta.pre_consulta": "Pré-consulta preenchida pelo paciente",
  "cirurgia.prontidao": "Prontidão para faturamento verificada",
  "impacto.linha_de_base": "Linha de base de tempo informada (Impacto)",
  "gestao.resumo_semanal": "Resumo semanal da gestão gerado",
  "gestao.erro_llm": "Erro do LLM no resumo da gestão",
  "consulta.quebra_de_vidro": "Acesso por quebra de vidro (consulta sem vínculo, com justificativa)",
  "consulta.acesso_negado": "Acesso à consulta negado (médico sem vínculo)",
  "consulta.nota_visualizada": "Nota da consulta visualizada",
  "consulta.documento_visualizado": "Documento da consulta visualizado",
  "paciente.visualizado": "Paciente aberto",
  "paciente.timeline_visualizada": "Linha do tempo do paciente visualizada",
  "audit.cadeia_verificada": "Cadeia de integridade da auditoria verificada",
  "audit.exportado": "Auditoria exportada (JSON ou CSV assinado)",
  "compliance.dossie_gerado": "Dossiê de conformidade gerado",
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
  { value: "paciente.", label: "Leituras de prontuário" },
  { value: "consulta.quebra_de_vidro", label: "Quebras de vidro" },
  { value: "audit.", label: "Verificações e exportações da auditoria" },
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

function IntegrityCard({ since, until }: { since: string; until: string }) {
  const [status, setStatus] = useState<ChainStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Falha na operação");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck aria-hidden className="size-5 text-primary" /> Integridade e exportação
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Cada registro guarda o hash do anterior. Alterar ou apagar uma linha quebra a cadeia e a verificação aponta a primeira linha afetada.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={busy} onClick={() => void run(async () => setStatus(await api<ChainStatus>("/audit/verify")))}>
            <ShieldCheck aria-hidden /> Verificar cadeia
          </Button>
          <Button variant="outline" disabled={busy} onClick={() => void run(() => downloadFile(`/audit/export?formato=csv&since=${since}&until=${until}`, "auditoria-housia.csv"))}>
            <Download aria-hidden /> Exportar CSV
          </Button>
          <Button variant="outline" disabled={busy} onClick={() => void run(() => downloadFile(`/audit/export?formato=json&since=${since}&until=${until}`, "auditoria-housia.json"))}>
            <Download aria-hidden /> Exportar JSON assinado
          </Button>
          <Button asChild variant="outline">
            <Link href="/conformidade">Dossiê de conformidade</Link>
          </Button>
        </div>
        {error && <Alert variant="critico">{error}</Alert>}
        {status && (
          <Alert variant={status.integra ? "ok" : "critico"} role="status">
            {status.integra
              ? `Cadeia íntegra: ${status.encadeadas} registro(s) encadeado(s), ${status.anteriores_ao_encadeamento} anterior(es) ao encadeamento.`
              : `Cadeia QUEBRADA no registro ${status.primeira_quebra?.id}: ${status.primeira_quebra?.motivo}.`}
          </Alert>
        )}
      </CardContent>
    </Card>
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
    case "consulta.modelo_nota":
      return `${p.de ?? "—"} → ${p.para ?? "—"} (v${p.versao ?? "?"})`;
    case "consulta.nota_revisada":
      return `${p.editado_pct ?? 0}% do texto editado (${p.palavras_alteradas ?? 0} palavra(s))`;
    case "consulta.envio_com_pendencia":
      return `${p.quantidade ?? 0} pendência(s) crítica(s) justificada(s)`;
    case "consulta.propostas":
      return `${(p.aceitas as string[] | undefined)?.length ?? 0} proposta(s) aceita(s)`;
    case "consulta.envio_erro":
      return `tentativa ${p.tentativa ?? "?"}: ${p.erro ?? ""}`;
    case "consulta.enviada":
    case "consulta.exportada":
      return p.tempo_ate_envio_s == null ? "" : `${Math.round(Number(p.tempo_ate_envio_s) / 60)} min após a finalização`;
    case "cirurgia.prontidao":
      return `${p.momento}: ${(p.antes as { score?: number } | null)?.score ?? "—"} → ${(p.depois as { score?: number }).score} pontos`;
    case "impacto.linha_de_base":
      return `${p.setor}: ${p.minutos_antes} min por atendimento antes do HousIA`;
    case "consulta.quebra_de_vidro":
      return `“${p.motivo}” (vale até ${p.valido_ate})`;
    case "audit.exportado":
      return `${p.formato} · ${p.linhas} linha(s) · SHA-256 ${String(p.arquivo_sha256 ?? "").slice(0, 12)}…`;
    case "audit.cadeia_verificada":
      return p.integra ? `íntegra (${p.total} registros)` : `QUEBRADA no registro ${p.primeira_quebra}`;
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

      <IntegrityCard since={since} until={until} />

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
