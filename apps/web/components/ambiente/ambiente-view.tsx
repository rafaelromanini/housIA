"use client";

import { CircleCheck, Info, OctagonAlert, RefreshCw, ShieldOff } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { AmbienteItem, ConsultConfig } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useApi } from "@/lib/use-api";

const ESTADO: Record<AmbienteItem["estado"], { label: string; variant: "ok" | "atencao" | "info"; Icon: typeof Info }> = {
  ok: { label: "Em conformidade", variant: "ok", Icon: CircleCheck },
  atencao: { label: "Atenção", variant: "atencao", Icon: OctagonAlert },
  info: { label: "Informativo", variant: "info", Icon: Info },
};

/** Tela "Ambiente" (#32): a configuração em vigor, lida do servidor, para o gestor conferir antes de liberar o uso. */
export function AmbienteView() {
  const { user } = useAuth();
  const allowed = user?.role === "gestor";
  const cfg = useApi<ConsultConfig>(allowed ? "/consult/config" : null);

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden />A tela Ambiente é restrita ao perfil de gestão.
      </Alert>
    );
  }
  const amb = cfg.data?.ambiente;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Ambiente</h1>
          <p className="text-sm text-muted-foreground">O que está configurado agora: para onde vão a voz, o texto e a nota.</p>
        </div>
        <Button variant="outline" onClick={() => void cfg.reload()} disabled={cfg.loading}>
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>
      {cfg.error && <Alert variant="critico">{cfg.error}</Alert>}
      {!amb && !cfg.error && <Skeleton className="h-64 w-full" />}
      {amb && (
        <>
          <div className="flex flex-wrap gap-2" aria-label="Resumo do ambiente">
            <Badge variant={amb.producao ? "ok" : "atencao"}>{amb.producao ? "Produção" : "Demonstração"}</Badge>
            <Badge variant="outline">IA: {amb.llm_mode === "real" ? "externa" : "mock (regras locais)"}</Badge>
            <Badge variant="outline">Prontuário: {amb.fhir_mode === "hapi" ? "servidor FHIR" : "local sintético"}</Badge>
          </div>
          <ul className="grid gap-3 md:grid-cols-2" aria-label="Checklist de transparência">
            {amb.checklist.map((it) => {
              const e = ESTADO[it.estado];
              return (
                <li key={it.id}>
                  <Card className="h-full">
                    <CardHeader className="flex-row items-center justify-between gap-2">
                      <CardTitle className="text-base">{it.titulo}</CardTitle>
                      <Badge variant={e.variant}>
                        <e.Icon aria-hidden /> {e.label}
                      </Badge>
                    </CardHeader>
                    <CardContent className="text-sm break-words" data-testid={`ambiente-${it.id}`}>
                      {it.texto}
                    </CardContent>
                  </Card>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}
