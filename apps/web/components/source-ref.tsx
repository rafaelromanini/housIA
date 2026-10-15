"use client";

import Link from "next/link";
import { useState } from "react";
import { FileSearch } from "lucide-react";

import { KIND_META } from "@/components/timeline";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { ClinicalEvent } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { fetchTimeline } from "@/lib/timeline-cache";

/** Link "ver fonte": mostra o(s) evento(s) FHIR que sustentam uma afirmação. */
export function SourceRefs({ patientId, refs, label = "ver fonte" }: { patientId: string; refs: string[]; label?: string }) {
  const [events, setEvents] = useState<ClinicalEvent[] | null>(null);

  async function onOpen(open: boolean) {
    if (open && !events) {
      const all = await fetchTimeline(patientId);
      setEvents(all.filter((e) => refs.includes(e.source_ref)));
    }
  }

  if (refs.length === 0) return null;
  return (
    <Dialog onOpenChange={(o) => void onOpen(o)}>
      <DialogTrigger asChild>
        <Button variant="link" size="sm" className="min-h-9 text-xs">
          <FileSearch aria-hidden />
          {label} ({refs.length})
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Fonte da afirmação</DialogTitle>
          <DialogDescription>Registros do prontuário (FHIR) citados. Confira antes de agir.</DialogDescription>
        </DialogHeader>
        {!events && <Skeleton className="h-24" />}
        {events && (
          <ul className="space-y-2">
            {refs.map((ref) => {
              const matches = events.filter((e) => e.source_ref === ref);
              return (
                <li key={ref} className="rounded-md border p-3 text-sm">
                  <p className="font-mono text-xs text-muted-foreground">{ref}</p>
                  {matches.length === 0 && (
                    <p className="mt-1 text-muted-foreground">Registro de contexto (diagnóstico, alergia ou prescrição ativa).</p>
                  )}
                  {matches.map((e) => (
                    <div key={e.id} className="mt-1 space-y-1">
                      <p className="text-xs text-muted-foreground">
                        {fmtDateTime(e.timestamp)} · {KIND_META[e.kind].label}
                        {e.location ? ` · ${e.location}` : ""}
                      </p>
                      <p>{e.summary}</p>
                      {e.text && <p className="whitespace-pre-wrap rounded bg-muted p-2">{e.text}</p>}
                    </div>
                  ))}
                  <Link
                    href={`/pacientes/${patientId}?ref=${encodeURIComponent(ref)}`}
                    className="mt-2 inline-flex min-h-9 items-center text-xs font-bold text-primary hover:underline"
                  >
                    Abrir na timeline
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
