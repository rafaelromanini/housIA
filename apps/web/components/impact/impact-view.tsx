"use client";

import { useMemo, useState } from "react";
import { Download, RefreshCw, ShieldOff } from "lucide-react";

import { DrillDialog } from "@/components/audit/drill-dialog";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, downloadFile, type AuditMetrics, type ImpactBaseline, type ImpactMetric } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dayOf } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const isoDay = (d: Date) => dayOf(d.toISOString());
const num = (v: number | null, unit: string) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}${unit === "%" ? "%" : ""}`;
const UNIT_LABEL: Record<string, string> = { min: "min", h: "h", pontos: "pontos", atendimentos: "", notas: "", consultas: "" };

function MetricCard({ id, m, onOpen }: { id: string; m: ImpactMetric; onOpen: (id: string) => void }) {
  return (
    <div className="flex flex-col rounded-lg border bg-card p-3 shadow-sm">
      <p className="text-sm text-muted-foreground">{m.rotulo}</p>
      <button
        type="button"
        onClick={() => onOpen(id)}
        aria-label={`${m.rotulo}: ${num(m.valor, m.unidade)}. Ver as ${m.n_linhas} linhas que compõem o número`}
        className="mt-1 min-h-11 self-start rounded-md text-left text-3xl font-bold leading-none text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {num(m.valor, m.unidade)}
        {m.unidade !== "%" && UNIT_LABEL[m.unidade] ? <span className="ml-1 text-base font-normal">{UNIT_LABEL[m.unidade]}</span> : null}
      </button>
      {m.denominador != null && (
        <p className="text-xs text-muted-foreground">
          {m.numerador} de {m.denominador}
        </p>
      )}
      {m.por_camada && (
        <ul className="mt-1 text-xs text-muted-foreground">
          {Object.entries(m.por_camada).map(([layer, c]) => (
            <li key={layer}>
              Camada {layer === "ia" ? "IA" : "regra"}: {c.aceitas} de {c.sugeridas} ({num(c.pct, "%")})
            </li>
          ))}
        </ul>
      )}
      {m.distribuicao && (
        <p className="mt-1 text-xs text-muted-foreground">
          {m.distribuicao.promotores} promotor(es) · {m.distribuicao.neutros} neutro(s) · {m.distribuicao.detratores} detrator(es)
        </p>
      )}
      {m.antes !== undefined && (
        <p className="mt-1 text-xs text-muted-foreground">
          Antes {num(m.antes ?? null, "pontos")} → depois {num(m.depois ?? null, "pontos")}
          {m.glosas_evitadas ? ` · ${m.glosas_evitadas} glosa(s) evitada(s)` : ""}
        </p>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        <strong>Fórmula:</strong> {m.formula}
      </p>
      <p className="text-xs text-muted-foreground">{m.n_linhas} linha(s) do audit_log</p>
    </div>
  );
}

function BaselineForm({ onSaved }: { onSaved: () => void }) {
  const config = useApi<{ especialidades: { value: string; label: string }[] }>("/consult/config");
  const baselines = useApi<ImpactBaseline[]>("/audit/impact/baseline");
  const [sector, setSector] = useState("clinica_medica");
  const [minutes, setMinutes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api("/audit/impact/baseline", { method: "POST", json: { setor: sector, minutos_antes: Number(minutes.replace(",", ".")) } });
      setMinutes("");
      await baselines.reload();
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível salvar.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Linha de base por setor</CardTitle>
        <p className="text-sm text-muted-foreground">
          Informe quanto tempo, em média, a documentação de um atendimento levava antes do HousIA. O painel calcula os minutos e as
          horas devolvidas à assistência a partir dela.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="bl-setor">Setor</Label>
            <NativeSelect id="bl-setor" value={sector} onChange={(e) => setSector(e.target.value)}>
              {(config.data?.especialidades ?? [{ value: "clinica_medica", label: "Clínica médica" }]).map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="bl-min">Tempo médio antes (min)</Label>
            <Input id="bl-min" inputMode="decimal" value={minutes} onChange={(e) => setMinutes(e.target.value)} placeholder="ex.: 15" />
          </div>
          <Button type="submit" disabled={busy || !minutes.trim()}>
            Salvar linha de base
          </Button>
        </form>
        {error && <Alert variant="critico">{error}</Alert>}
        {baselines.data && baselines.data.length > 0 && (
          <ul className="text-sm">
            {baselines.data.map((b) => (
              <li key={b.id}>
                <strong>{b.rotulo}:</strong> {b.minutos_antes.toLocaleString("pt-BR")} min por atendimento
                {b.por ? ` (informado por ${b.por})` : ""}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** Tela "Impacto": adoção, tempo e qualidade da consulta com IA; cada número com fórmula e as linhas que o compõem. */
export function ImpactView() {
  const { user } = useAuth();
  const today = new Date();
  const [since, setSince] = useState(isoDay(new Date(today.getTime() - 30 * 86400000)));
  const [until, setUntil] = useState(isoDay(today));
  const [minUse, setMinUse] = useState("3");
  const [drill, setDrill] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const allowed = user?.role === "gestor";
  const range = useMemo(() => `since=${since}&until=${until}`, [since, until]);
  const metrics = useApi<AuditMetrics>(allowed ? `/audit/metrics?${range}&uso_regular_min=${Number(minUse) || 3}` : null);

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden />O painel Impacto é restrito ao perfil de gestão.
      </Alert>
    );
  }
  const report = metrics.data?.consulta_ia;

  async function exportFile(path: string, name: string) {
    setExportError(null);
    try {
      await downloadFile(path, name);
    } catch (e) {
      setExportError(e instanceof Error ? e.message : "Não foi possível exportar.");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Impacto</h1>
          <p className="text-sm text-muted-foreground">
            Adoção, tempo e qualidade da consulta com IA. Clique num número para ver as linhas do audit_log que o compõem.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void exportFile(`/audit/metrics/export.csv?${range}`, "impacto-housia.csv")}>
            <Download aria-hidden /> CSV
          </Button>
          <Button variant="outline" onClick={() => void exportFile(`/audit/metrics/measure-report?${range}`, "impacto-measurereport.json")}>
            <Download aria-hidden /> FHIR MeasureReport
          </Button>
          <Button variant="outline" onClick={() => void metrics.reload()}>
            <RefreshCw aria-hidden /> Atualizar
          </Button>
        </div>
      </div>
      {exportError && <Alert variant="critico">{exportError}</Alert>}

      <Card>
        <CardContent className="pt-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="imp-desde">De</Label>
              <Input id="imp-desde" type="date" value={since} max={until} onChange={(e) => setSince(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="imp-ate">Até</Label>
              <Input id="imp-ate" type="date" value={until} min={since} onChange={(e) => setUntil(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="imp-uso">Uso regular: consultas por semana</Label>
              <Input id="imp-uso" type="number" min={1} max={100} value={minUse} onChange={(e) => setMinUse(e.target.value)} />
            </div>
          </div>
        </CardContent>
      </Card>

      {metrics.error && <Alert variant="critico">{metrics.error}</Alert>}
      {!report && !metrics.error && <Skeleton className="h-64" />}
      {report?.grupos.map((g) => (
        <section key={g.id} aria-label={g.rotulo} className="space-y-2">
          <h2 className="text-lg font-bold">{g.rotulo}</h2>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {g.metricas.map((key) => (
              <MetricCard key={key} id={key} m={report.metricas[key]} onOpen={setDrill} />
            ))}
          </div>
          {g.id === "volume" && report.metricas.consultas_por_profissional_mes.tabela?.length ? (
            <div className="overflow-x-auto rounded-lg border bg-card">
              <table className="w-full min-w-[420px] text-sm">
                <caption className="p-2 text-left text-xs text-muted-foreground">Consultas por profissional por mês</caption>
                <thead>
                  <tr className="border-b text-left">
                    <th scope="col" className="p-2">Profissional</th>
                    <th scope="col" className="p-2">Mês</th>
                    <th scope="col" className="p-2 text-right">Consultas</th>
                  </tr>
                </thead>
                <tbody>
                  {report.metricas.consultas_por_profissional_mes.tabela.map((t) => (
                    <tr key={`${t.profissional}-${t.mes}`} className="border-b">
                      <td className="p-2">{t.profissional}</td>
                      <td className="p-2 tabular-nums">{t.mes}</td>
                      <td className="p-2 text-right tabular-nums">{t.consultas}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {g.id === "satisfacao" && report.metricas.horas_devolvidas.por_setor?.length ? (
            <div className="overflow-x-auto rounded-lg border bg-card">
              <table className="w-full min-w-[560px] text-sm">
                <caption className="p-2 text-left text-xs text-muted-foreground">
                  Horas devolvidas por setor (sem linha de base, o setor não entra na conta)
                </caption>
                <thead>
                  <tr className="border-b text-left">
                    <th scope="col" className="p-2">Setor</th>
                    <th scope="col" className="p-2 text-right">Atendimentos</th>
                    <th scope="col" className="p-2 text-right">Antes (min)</th>
                    <th scope="col" className="p-2 text-right">Hoje (min)</th>
                    <th scope="col" className="p-2 text-right">Devolvidos (min)</th>
                  </tr>
                </thead>
                <tbody>
                  {report.metricas.horas_devolvidas.por_setor.map((s) => (
                    <tr key={s.setor} className="border-b">
                      <td className="p-2">{s.rotulo}</td>
                      <td className="p-2 text-right tabular-nums">{s.atendimentos}</td>
                      <td className="p-2 text-right tabular-nums">{s.linha_de_base_min ?? "sem linha de base"}</td>
                      <td className="p-2 text-right tabular-nums">{s.tempo_atual_min ?? "—"}</td>
                      <td className="p-2 text-right tabular-nums">{s.minutos_devolvidos ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      ))}

      <BaselineForm onSaved={() => void metrics.reload()} />
      <DrillDialog kind="audit" path={drill ? `/audit/metrics/${drill}/linhas?${range}&uso_regular_min=${Number(minUse) || 3}` : null} onClose={() => setDrill(null)} />
    </div>
  );
}
