"use client";

import Link from "next/link";
import { CalendarClock, ChevronRight, Mic, RefreshCw, Sparkles } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { Appointment } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const FHIR_STATUS: Record<string, { label: string; variant: "info" | "atencao" | "ok" | "outline" }> = {
  planned: { label: "Agendado", variant: "outline" },
  arrived: { label: "Aguardando", variant: "atencao" },
  triaged: { label: "Triado", variant: "atencao" },
  "in-progress": { label: "Em atendimento", variant: "info" },
  finished: { label: "Atendido", variant: "ok" },
};
const HOUSIA_STATUS = {
  em_andamento: "Consulta com IA em andamento",
  finalizada: "Nota aguardando revisão",
  enviada: "Nota no prontuário",
} as const;

function age(birth: string | null) {
  if (!birth) return null;
  const b = new Date(birth);
  const n = new Date();
  return n.getFullYear() - b.getFullYear() - (n < new Date(n.getFullYear(), b.getMonth(), b.getDate()) ? 1 : 0);
}

/** Agenda ambulatorial do dia (lida do FHIR) com atalho para a consulta com IA. */
export function Agenda() {
  const { data, error, loading, reload } = useApi<Appointment[]>("/appointments");
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <CalendarClock aria-hidden className="size-6 text-primary" /> Agenda de consultas
          </h1>
          <p className="text-sm text-muted-foreground">Hoje · lida do prontuário (FHIR). A consulta com IA transcreve e sugere perguntas em tempo real.</p>
        </div>
        <Button variant="outline" onClick={() => void reload()} disabled={loading}>
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-40 w-full" />}
      {data && data.length === 0 && <p className="text-sm text-muted-foreground">Nenhuma consulta agendada para hoje.</p>}
      {data && data.length > 0 && (
        <ul className="space-y-2">
          {data.map((a) => {
            const st = FHIR_STATUS[a.status_fhir] ?? { label: a.status_fhir, variant: "outline" as const };
            return (
              <li key={a.encounter_id}>
                <Card>
                  <CardContent className="flex flex-wrap items-center gap-3 pt-4">
                    <span className="w-16 font-mono text-lg font-bold tabular-nums">
                      {fmtTime(a.horario)}
                    </span>
                    <div className="min-w-48 flex-1">
                      <p className="flex flex-wrap items-center gap-2 font-bold">
                        {a.nome}
                        {age(a.nascimento) !== null && <span className="text-sm font-normal text-muted-foreground">{age(a.nascimento)} anos</span>}
                        {a.cenario && (
                          <Badge variant="ai" className="text-[11px]">
                            <Sparkles aria-hidden /> cenário da demo
                          </Badge>
                        )}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {a.motivo} · {a.servico}
                        {a.profissional && ` · ${a.profissional}`}
                      </p>
                      {a.consulta_status && <p className="text-xs font-bold text-primary">{HOUSIA_STATUS[a.consulta_status]}</p>}
                    </div>
                    <Badge variant={st.variant}>{st.label}</Badge>
                    <Button asChild variant={a.consulta_status === "em_andamento" ? "outline" : "default"}>
                      <Link href={`/pacientes/${a.patient_id}/consulta`}>
                        <Mic aria-hidden />
                        {a.consulta_status === "em_andamento" ? "Continuar" : a.consulta_status ? "Abrir" : "Iniciar consulta com IA"}
                        <ChevronRight aria-hidden />
                      </Link>
                    </Button>
                  </CardContent>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
