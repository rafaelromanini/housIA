"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { JourneyMetrics, JourneyMetricsRow } from "@/lib/api";
import { fmtDay } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`);

function Row({ label, row, strong }: { label: string; row: JourneyMetricsRow; strong?: boolean }) {
  return (
    <tr className={strong ? "border-t-2 font-bold" : "border-t"}>
      <th scope="row" className="py-2 pr-3 text-left font-bold">
        {label}
      </th>
      <td className="px-2 py-2 text-right">{row.altas}</td>
      <td className="px-2 py-2 text-right">{row.seguimento_completo}</td>
      <td className="px-2 py-2 text-right">
        {row.reinternacoes} <span className="text-muted-foreground">({pct(row.taxa_reinternacao)})</span>
      </td>
      <td className="px-2 py-2 text-right">
        {row.retornos_perdidos} <span className="text-muted-foreground">({pct(row.taxa_retorno_perdido)})</span>
      </td>
      <td className="px-2 py-2 text-right">{row.faltas}</td>
    </tr>
  );
}

/** Reinternação em 30 dias e retornos perdidos por coorte, calculados sobre os Encounter IMP, com os casos à mostra. */
export function MetricsPanel() {
  const [cohort, setCohort] = useState("risco");
  const [days, setDays] = useState("365");
  const { data, error, loading, reload } = useApi<JourneyMetrics>(`/journey/metrics?coorte=${cohort}&dias=${days}`);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="m-coorte">Coorte</Label>
          <NativeSelect id="m-coorte" value={cohort} onChange={(e) => setCohort(e.target.value)}>
            <option value="risco">Risco de reinternação (LACE)</option>
            <option value="mes">Mês da alta</option>
            <option value="cid">Diagnóstico principal (CID-10)</option>
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="m-dias">Altas dos últimos</Label>
          <NativeSelect id="m-dias" value={days} onChange={(e) => setDays(e.target.value)}>
            <option value="90">90 dias</option>
            <option value="180">180 dias</option>
            <option value="365">365 dias</option>
          </NativeSelect>
        </div>
        <Button variant="outline" onClick={() => void reload()} disabled={loading}>
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-48 w-full" />}
      {data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Reinternação em {data.parametros.reinternacao_dias} dias e retornos perdidos</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[34rem] text-sm">
                  <caption className="sr-only">Métricas da jornada por coorte</caption>
                  <thead className="text-xs text-muted-foreground">
                    <tr>
                      <th scope="col" className="py-1 pr-3 text-left">
                        Coorte
                      </th>
                      <th scope="col" className="px-2 text-right">
                        Altas
                      </th>
                      <th scope="col" className="px-2 text-right">
                        Seguimento completo
                      </th>
                      <th scope="col" className="px-2 text-right">
                        Reinternações
                      </th>
                      <th scope="col" className="px-2 text-right">
                        Retornos perdidos
                      </th>
                      <th scope="col" className="px-2 text-right">
                        Faltas
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.coortes.map((c) => (
                      <Row key={c.chave} label={c.rotulo} row={c} />
                    ))}
                    <Row label="Total" row={data.total} strong />
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-muted-foreground">{data.metodo}</p>
              {data.total.em_seguimento > 0 && (
                <p className="text-xs text-muted-foreground">
                  {data.total.em_seguimento} alta(s) com menos de {data.parametros.reinternacao_dias} dias ainda estão em seguimento e não entram nas taxas.
                </p>
              )}
            </CardContent>
          </Card>
          <details className="rounded-lg border bg-card">
            <summary className="min-h-11 cursor-pointer px-4 py-3 text-sm font-bold">Casos que sustentam a conta ({data.casos.length})</summary>
            <div className="overflow-x-auto px-4 pb-4">
              <table className="w-full min-w-[40rem] text-sm">
                <caption className="sr-only">Altas consideradas, com a referência do Encounter</caption>
                <thead className="text-xs text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 pr-3 text-left">
                      Paciente
                    </th>
                    <th scope="col" className="px-2 text-left">
                      Alta
                    </th>
                    <th scope="col" className="px-2 text-left">
                      Encounter IMP
                    </th>
                    <th scope="col" className="px-2 text-left">
                      LACE
                    </th>
                    <th scope="col" className="px-2 text-left">
                      Reinternação
                    </th>
                    <th scope="col" className="px-2 text-left">
                      Retorno
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.casos.map((c) => (
                    <tr key={c.encounter_ref} className="border-t align-top">
                      <th scope="row" className="py-2 pr-3 text-left font-bold">
                        {c.paciente}
                      </th>
                      <td className="px-2 py-2">{fmtDay(c.alta_em)}</td>
                      <td className="px-2 py-2 font-mono text-xs">{c.encounter_ref}</td>
                      <td className="px-2 py-2">
                        {c.lace} <Badge variant={c.risco === "alto" ? "critico" : c.risco === "moderado" ? "atencao" : "ok"}>{c.risco}</Badge>
                      </td>
                      <td className="px-2 py-2">
                        {c.reinternado ? `${c.dias_ate_reinternacao} dia(s) · ${c.reinternacao_ref}` : c.seguimento_completo ? "não" : "em seguimento"}
                      </td>
                      <td className="px-2 py-2">
                        {c.retorno_realizado ? `feito · ${c.retorno_ref}` : c.falta_ref ? `faltou · ${c.falta_ref}` : c.seguimento_completo ? "perdido" : "em seguimento"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </div>
  );
}
