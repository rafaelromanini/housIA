"use client";

import { useEffect, useRef } from "react";
import { CheckCircle2, ClipboardList } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { PreConsulta, PreConsultaLine } from "@/lib/api";
import { useApi } from "@/lib/use-api";

function Lines({ patientId, lines }: { patientId: string; lines: PreConsultaLine[] }) {
  return (
    <ul className="space-y-1">
      {lines.map((l, i) => (
        <li key={`${l.tipo}-${i}`} className={l.tratada ? "text-muted-foreground" : undefined}>
          <span className={l.tratada ? "line-through" : undefined}>{l.texto}</span>{" "}
          {l.tratada && (
            <span className="inline-flex items-center gap-1 text-xs font-bold text-ok">
              <CheckCircle2 aria-hidden className="size-3.5" /> tratada na conversa
            </span>
          )}{" "}
          <SourceRefs patientId={patientId} refs={[...l.refs, ...l.tratada_refs]} />
          <span className="block text-xs text-muted-foreground">Fonte: {l.fonte}</span>
        </li>
      ))}
    </ul>
  );
}

function Block({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return (
    <section aria-label={title} data-testid={testId}>
      <h4 className="text-xs font-bold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <div className="mt-1 text-sm">{children}</div>
    </section>
  );
}

/**
 * Cartão "Antes de entrar": motivo, o que mudou desde a última consulta, pendências e resumo da última alta.
 * Com `consultaId`, as pendências citadas na conversa aparecem como tratadas (recarrega a cada fala nova).
 */
export function PreConsultaCard({
  patientId,
  consultaId,
  falas = 0,
  defaultOpen = true,
  resumo = false,
}: {
  patientId: string;
  consultaId?: number;
  falas?: number;
  defaultOpen?: boolean;
  resumo?: boolean;
}) {
  const qs = new URLSearchParams();
  if (consultaId) qs.set("consulta", String(consultaId));
  if (resumo) qs.set("resumo", "true");
  const query = qs.toString();
  const { data, error, loading, reload } = useApi<PreConsulta>(`/patients/${patientId}/pre-consulta${query ? `?${query}` : ""}`);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    if (consultaId) void reload();
  }, [falas, consultaId, reload]);

  const open = data ? data.pendencias.filter((p) => !p.tratada).length : 0;
  return (
    <details className="rounded-lg border bg-card text-sm" open={defaultOpen} data-testid="antes-de-entrar">
      <summary className="flex min-h-11 cursor-pointer items-center gap-2 px-3 font-medium">
        <ClipboardList aria-hidden className="size-4 text-primary" /> Antes de entrar
        {data && open > 0 && <Badge variant="atencao">{open} pendência(s)</Badge>}
      </summary>
      <div className="space-y-3 border-t px-3 py-3">
        {error && <Alert variant="atencao">{error}</Alert>}
        {!data && loading && <Skeleton className="h-24 w-full" />}
        {data && (
          <>
            {data.resumo_ia.length > 0 && (
              <Block title="Resumo" testId="pre-resumo">
                <p>
                  {data.resumo_ia.map((f) => f.texto).join(" ")}{" "}
                  <span className="text-xs text-muted-foreground">({data.modo_resumo === "ia" ? "IA" : "regras"})</span>
                </p>
              </Block>
            )}
            {data.motivo.length > 0 && (
              <Block title="Motivo" testId="pre-motivo">
                <Lines patientId={patientId} lines={data.motivo} />
              </Block>
            )}
            <Block title={data.ultima_consulta ? `O que mudou desde ${data.ultima_consulta}` : "O que mudou"} testId="pre-mudou">
              {data.mudou.length > 0 ? (
                <Lines patientId={patientId} lines={data.mudou} />
              ) : (
                <p className="text-muted-foreground">
                  {data.ultima_consulta ? "Nada novo no prontuário desde a última consulta." : "Sem consulta anterior no prontuário para comparar."}
                </p>
              )}
            </Block>
            <Block title="Pendências" testId="pre-pendencias">
              {data.pendencias.length > 0 ? (
                <Lines patientId={patientId} lines={data.pendencias} />
              ) : (
                <p className="text-muted-foreground">Nenhuma pendência registrada.</p>
              )}
            </Block>
            {data.ultima_alta.length > 0 && (
              <Block title="Última alta" testId="pre-alta">
                <Lines patientId={patientId} lines={data.ultima_alta} />
              </Block>
            )}
            <p className="text-xs text-muted-foreground">{data.aviso}</p>
          </>
        )}
      </div>
    </details>
  );
}
