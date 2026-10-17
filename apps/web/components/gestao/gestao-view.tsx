"use client";

import { useMemo, useState } from "react";
import { Download, RefreshCw, ShieldOff, Sparkles } from "lucide-react";

import { DrillDialog } from "@/components/audit/drill-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api, downloadFile, type GestaoIndicator, type GestaoReport, type GestaoSummary } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dayOf } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const isoDay = (d: Date) => dayOf(d.toISOString());
const fmt = (v: number | null, unit: string) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}${unit === "%" ? "%" : ""}`;

function Spark({ points }: { points: { semana: string; valor: number | null }[] }) {
  const max = Math.max(1, ...points.map((p) => p.valor ?? 0));
  return (
    <ol className="mt-2 flex items-end gap-1" aria-label="Série semanal">
      {points.map((p) => (
        <li key={p.semana} className="flex flex-1 flex-col items-center text-[10px] text-muted-foreground">
          <span className="tabular-nums">{p.valor ?? "—"}</span>
          <span className="w-full rounded-sm bg-primary/70" style={{ height: `${Math.max(2, ((p.valor ?? 0) / max) * 32)}px` }} />
          <span>{p.semana.slice(-3)}</span>
        </li>
      ))}
    </ol>
  );
}

function IndicatorCard({
  id,
  ind,
  serie,
  onOpen,
}: {
  id: string;
  ind: GestaoIndicator;
  serie?: { semana: string; valor: number | null }[];
  onOpen: (id: string) => void;
}) {
  return (
    <div className="flex flex-col rounded-lg border bg-card p-3 shadow-sm">
      <p className="text-sm text-muted-foreground">{ind.rotulo}</p>
      <button
        type="button"
        onClick={() => onOpen(id)}
        aria-label={`${ind.rotulo}: ${fmt(ind.valor, ind.unidade)}. Ver as ${ind.n_linhas} linhas que compõem o número`}
        className="mt-1 min-h-11 self-start rounded-md text-left text-3xl font-bold leading-none text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {fmt(ind.valor, ind.unidade)}
        {ind.unidade !== "%" && <span className="ml-1 text-base font-normal">{ind.unidade}</span>}
      </button>
      {ind.denominador != null && (
        <p className="text-xs text-muted-foreground">
          {ind.numerador} de {ind.denominador}
        </p>
      )}
      {Object.keys(ind.por_setor).length > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {Object.entries(ind.por_setor)
            .map(([s, v]) => `${s}: ${v.toLocaleString("pt-BR")}`)
            .join(" · ")}
        </p>
      )}
      {ind.por_transicao && Object.keys(ind.por_transicao).length > 0 && (
        <ul className="mt-1 text-xs text-muted-foreground">
          {Object.entries(ind.por_transicao).map(([t, v]) => (
            <li key={t}>
              {t}: {v}
            </li>
          ))}
        </ul>
      )}
      {ind.ranking && ind.ranking.length > 0 && (
        <ol className="mt-1 text-xs text-muted-foreground">
          {ind.ranking.map((r) => (
            <li key={r.cid}>
              {r.cid} — {r.descricao}: {r.n}
            </li>
          ))}
        </ol>
      )}
      {serie && <Spark points={serie} />}
      <p className="mt-2 text-xs text-muted-foreground">
        <strong>Fórmula:</strong> {ind.formula}
      </p>
      <p className="text-xs text-muted-foreground">{ind.n_linhas} linha(s)</p>
    </div>
  );
}

/** Copiloto do gestor: indicadores de alta, transição, centro cirúrgico e ambulatório, cada número com as linhas e os recursos FHIR. */
export function GestaoView() {
  const { user } = useAuth();
  const today = new Date();
  const [since, setSince] = useState(isoDay(new Date(today.getTime() - 7 * 86400000)));
  const [until, setUntil] = useState(isoDay(today));
  const [drill, setDrill] = useState<string | null>(null);
  const [summary, setSummary] = useState<GestaoSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allowed = user?.role === "gestor";
  const range = useMemo(() => `since=${since}&until=${until}`, [since, until]);
  const panel = useApi<GestaoReport>(allowed ? `/gestao?${range}` : null);

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden />A tela de gestão é restrita ao perfil de gestão.
      </Alert>
    );
  }
  const r = panel.data;

  async function summarize() {
    setBusy(true);
    setError(null);
    try {
      setSummary(await api<GestaoSummary>(`/gestao/resumo-semanal?${range}`, { method: "POST" }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível gerar o resumo.");
    } finally {
      setBusy(false);
    }
  }

  async function exportFile(path: string, name: string) {
    setError(null);
    try {
      await downloadFile(path, name);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível exportar.");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Gestão</h1>
          <p className="text-sm text-muted-foreground">
            Alta, transição de cuidado, centro cirúrgico e ambulatório. Clique num número para ver os pacientes e recursos FHIR que o compõem.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void exportFile(`/gestao/export.csv?${range}`, "gestao-housia.csv")}>
            <Download aria-hidden /> CSV
          </Button>
          <Button variant="outline" onClick={() => void exportFile(`/gestao/measure-report?${range}`, "gestao-measurereport.json")}>
            <Download aria-hidden /> FHIR MeasureReport
          </Button>
          <Button variant="outline" onClick={() => void panel.reload()}>
            <RefreshCw aria-hidden /> Atualizar
          </Button>
        </div>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}

      <Card>
        <CardContent className="pt-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="g-desde">De</Label>
              <Input id="g-desde" type="date" value={since} max={until} onChange={(e) => setSince(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="g-ate">Até</Label>
              <Input id="g-ate" type="date" value={until} min={since} onChange={(e) => setUntil(e.target.value)} />
            </div>
          </div>
        </CardContent>
      </Card>

      {panel.error && <Alert variant="critico">{panel.error}</Alert>}
      {!r && !panel.error && <Skeleton className="h-64" />}
      {r?.grupos.map((g) => (
        <section key={g.id} aria-label={g.rotulo} className="space-y-2">
          <h2 className="text-lg font-bold">{g.rotulo}</h2>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {g.indicadores.map((key) => (
              <IndicatorCard key={key} id={key} ind={r.indicadores[key]} serie={r.serie_semanal[key]} onOpen={setDrill} />
            ))}
          </div>
        </section>
      ))}

      {r && r.comparacao_setores.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Comparação entre setores</CardTitle>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <caption className="sr-only">Indicadores por setor</caption>
              <thead>
                <tr className="border-b text-left">
                  <th scope="col" className="py-2 pr-3">Setor</th>
                  {Object.keys(r.indicadores)
                    .filter((k) => Object.keys(r.indicadores[k].por_setor).length > 0)
                    .map((k) => (
                      <th key={k} scope="col" className="py-2 pr-3 text-right text-xs">
                        {r.indicadores[k].rotulo}
                      </th>
                    ))}
                </tr>
              </thead>
              <tbody>
                {r.comparacao_setores.map((row) => (
                  <tr key={row.setor} className="border-b">
                    <th scope="row" className="py-2 pr-3 text-left font-bold">{row.setor}</th>
                    {Object.keys(r.indicadores)
                      .filter((k) => Object.keys(r.indicadores[k].por_setor).length > 0)
                      .map((k) => (
                        <td key={k} className="py-2 pr-3 text-right tabular-nums">
                          {row.valores[k] ?? "—"}
                        </td>
                      ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles aria-hidden className="size-5 text-primary" /> Resumo semanal
          </CardTitle>
          <p className="text-sm text-muted-foreground">
            Opcional. Cada frase cita o indicador de origem e só usa números que estão no painel; o que não confere é descartado.
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button onClick={() => void summarize()} disabled={busy || !r}>
            {busy ? "Gerando…" : "Gerar resumo semanal"}
          </Button>
          {summary && (
            <>
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {summary.frases.map((f) => (
                  <li key={f.texto}>
                    {f.texto}{" "}
                    {f.refs.map((ref) => (
                      <button
                        key={ref}
                        type="button"
                        onClick={() => setDrill(ref)}
                        className="inline-flex min-h-8 items-center rounded-md px-1 align-baseline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        aria-label={`Ver as linhas de ${r?.indicadores[ref]?.rotulo ?? ref}`}
                      >
                        <Badge variant="outline">{r?.indicadores[ref]?.rotulo ?? ref}</Badge>
                      </button>
                    ))}
                  </li>
                ))}
              </ul>
              {summary.frases.length === 0 && <p className="text-sm text-muted-foreground">Nada relevante para resumir no período.</p>}
              {summary.descartados.length > 0 && (
                <p className="text-xs text-muted-foreground">{summary.descartados.length} frase(s) descartada(s) por não conferir com o painel.</p>
              )}
              <p className="text-xs text-muted-foreground">
                {summary.meta.modo === "mock" ? "Modelo de texto (modo demonstração)" : summary.meta.modelo} · prompt v{summary.meta.prompt_version}
              </p>
            </>
          )}
        </CardContent>
      </Card>

      <DrillDialog kind="gestao" path={drill ? `/gestao/indicadores/${drill}/linhas?${range}` : null} onClose={() => setDrill(null)} />
    </div>
  );
}
