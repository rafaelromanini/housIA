"use client";

import { useEffect, useState } from "react";
import { CircleCheck, FileCheck, Loader2, Receipt } from "lucide-react";

import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type ConsultInsights, type Consultation, type ConvenioOption, type Readiness } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Prontidão para faturamento (0–100): regras de glosa em dados, com a fonte de cada pendência. */
export function ReadinessCard({
  readiness,
  patientId,
  consultId,
  numeroGuia,
  onInsights,
}: {
  readiness: Readiness;
  patientId: string;
  /** Com `consultId` e `onInsights` o convênio e o número da guia são editáveis (durante o ditado e a revisão). */
  consultId?: number;
  numeroGuia?: string | null;
  onInsights?: (insights: ConsultInsights | null, patch?: Partial<Consultation>) => void;
}) {
  const [convenios, setConvenios] = useState<ConvenioOption[]>([]);
  const [guia, setGuia] = useState(numeroGuia ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editable = consultId !== undefined && onInsights !== undefined;

  useEffect(() => {
    if (!editable) return;
    api<ConvenioOption[]>("/consult/convenios")
      .then(setConvenios)
      .catch(() => undefined);
  }, [editable]);
  useEffect(() => setGuia(numeroGuia ?? ""), [numeroGuia]);

  async function save(body: { convenio?: string; numero_guia?: string }) {
    if (!editable) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ insights?: ConsultInsights; faturamento: Consultation["faturamento"] }>(
        `/consultations/${consultId}/faturamento`,
        { method: "PUT", json: body },
      );
      onInsights(res.insights ?? null, { faturamento: res.faturamento });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao salvar");
    } finally {
      setBusy(false);
    }
  }

  const tone = readiness.pronto ? "text-ok" : readiness.criticas > 0 ? "text-critico" : "text-atencao";
  const bar = readiness.pronto ? "bg-ok" : readiness.criticas > 0 ? "bg-critico" : "bg-atencao";
  return (
    <Card className={cn(readiness.criticas > 0 && "border-critico-border")}>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          <Receipt aria-hidden className="size-5 text-primary" /> Pronto para faturar
          <span className={cn("text-2xl font-bold tabular-nums", tone)} data-testid="prontidao-score">
            {readiness.score}
            <span className="text-sm font-normal text-muted-foreground">/100</span>
          </span>
          {readiness.pronto ? (
            <Badge variant="ok">
              <CircleCheck aria-hidden /> Pronto
            </Badge>
          ) : (
            <Badge variant={readiness.criticas > 0 ? "critico" : "atencao"}>
              {readiness.criticas > 0 ? `${readiness.criticas} crítica(s)` : `${readiness.atencoes} de atenção`}
            </Badge>
          )}
        </CardTitle>
        <CardDescription>
          {readiness.convenio_rotulo}
          {readiness.procedimento && ` · ${readiness.procedimento}`}
          {readiness.codigo_demo && (
            <>
              {" "}
              · código <span className="font-mono">{readiness.codigo_demo}</span> (demonstração)
            </>
          )}{" "}
          · {readiness.regras_ok}/{readiness.regras_avaliadas} verificações · regras v{readiness.versao}
        </CardDescription>
        <div
          role="progressbar"
          aria-label="Prontidão para faturamento"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={readiness.score}
          className="h-2 overflow-hidden rounded-full bg-muted"
        >
          <div className={cn("h-full transition-all", bar)} style={{ width: `${readiness.score}%` }} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {editable && (
          <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <div className="space-y-1">
              <Label htmlFor="fat-convenio">Convênio</Label>
              <NativeSelect
                id="fat-convenio"
                value={readiness.convenio}
                disabled={busy}
                onChange={(e) => void save({ convenio: e.target.value })}
              >
                {(convenios.length ? convenios : [{ value: readiness.convenio, label: readiness.convenio_rotulo, codigos: "" }]).map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <div className="space-y-1">
              <Label htmlFor="fat-guia">Número da guia autorizada</Label>
              <Input id="fat-guia" value={guia} onChange={(e) => setGuia(e.target.value)} maxLength={60} placeholder="ex.: 778899" />
            </div>
            <Button variant="outline" disabled={busy || guia === (numeroGuia ?? "")} onClick={() => void save({ numero_guia: guia })}>
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <FileCheck aria-hidden />} Salvar
            </Button>
          </div>
        )}
        {error && <p className="text-sm font-bold text-critico">{error}</p>}
        {readiness.pendencias.length === 0 ? (
          <p className="inline-flex items-center gap-1 text-sm font-bold text-ok">
            <CircleCheck aria-hidden className="size-4" /> Sem pendências de faturamento.
          </p>
        ) : (
          <ul className="space-y-2">
            {readiness.pendencias.map((p, i) => (
              <li key={`${p.regra}-${p.campo ?? ""}-${i}`} className="flex gap-2 text-sm">
                <SeverityIcon severity={p.severidade} className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">
                  {p.texto} <SourceRefs patientId={patientId} refs={p.refs} />
                  <span className="block text-xs text-muted-foreground">
                    Regra {p.regra} · Fonte: {p.fonte}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
