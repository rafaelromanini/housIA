"use client";

import { ArrowRightLeft, ClipboardList, History, Pill, Users } from "lucide-react";

import { AlertsCard } from "@/components/patient/alerts-card";
import { usePatient } from "@/components/patient/patient-context";
import { Timeline } from "@/components/timeline";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ContextItem } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";

function ContextList({ items, empty }: { items: ContextItem[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="space-y-1.5 text-sm">
      {items.map((i) => (
        <li key={i.source_ref} className="leading-snug">
          {i.summary}
        </li>
      ))}
    </ul>
  );
}

export function PatientOverview({ highlightRef }: { highlightRef?: string | null }) {
  const { patient: p } = usePatient();
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
      <div className="space-y-4">
        <AlertsCard />
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ClipboardList aria-hidden className="size-4 text-primary" /> Diagnósticos ativos
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ContextList items={p.contexto.diagnosticos} empty="Sem diagnósticos registrados." />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Pill aria-hidden className="size-4 text-primary" /> Medicamentos ativos ({p.contexto.medicamentos_ativos.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ContextList items={p.contexto.medicamentos_ativos} empty="Sem prescrições ativas." />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ArrowRightLeft aria-hidden className="size-4 text-primary" /> Setores nesta internação
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="space-y-1.5 text-sm">
              {p.setores.map((s) => (
                <li key={s.encounter_id}>
                  <span className="font-bold">{s.setor}</span> · Leito {s.leito}
                  <br />
                  <span className="text-muted-foreground">
                    {s.inicio ? fmtDateTime(s.inicio) : "—"} → {s.fim ? fmtDateTime(s.fim) : "atual"}
                  </span>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Users aria-hidden className="size-4 text-primary" /> Rede de apoio
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ContextList items={p.contexto.familiares} empty="Sem familiar/cuidador registrado." />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <History aria-hidden className="size-4 text-primary" /> Idas ao PS antes desta internação
            </CardTitle>
          </CardHeader>
          <CardContent>
            {p.atendimentos_urgencia_anteriores.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nenhuma registrada.</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {p.atendimentos_urgencia_anteriores.map((a) => (
                  <li key={a.source_ref}>
                    <span className="font-bold tabular-nums">{fmtDateTime(a.inicio)}</span> — {a.motivo}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Timeline clínica</CardTitle>
        </CardHeader>
        <CardContent>
          <Timeline patientId={p.id} highlightRef={highlightRef} />
        </CardContent>
      </Card>
    </div>
  );
}
