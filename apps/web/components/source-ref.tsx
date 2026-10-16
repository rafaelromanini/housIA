"use client";

import Link from "next/link";
import { useState } from "react";
import { FileSearch, MessageSquareQuote } from "lucide-react";

import { KIND_META } from "@/components/timeline";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { ClinicalEvent } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { fetchTimeline } from "@/lib/timeline-cache";

/** Recursos que não viram evento da timeline: o que cada tipo significa. */
const CONTEXT_LABEL: Record<string, string> = {
  ServiceRequest: "Pedido no prontuário (cirurgia agendada ou exame solicitado/proposto).",
  Appointment: "Agendamento da sala cirúrgica.",
  Consent: "Termo de consentimento registrado no prontuário.",
  Device: "Implante registrado na cirurgia (etiqueta lida: lote, série e validade).",
};

/** Destaca uma fala da transcrição da consulta (refs "Fala/N"). */
export function focusFala(n: number) {
  window.dispatchEvent(new CustomEvent("housia:fala", { detail: n }));
}

/** Link "ver fonte": mostra o(s) evento(s) FHIR que sustentam uma afirmação; refs "Fala/N" apontam para a conversa. */
export function SourceRefs({ patientId, refs: allRefs, label = "ver fonte" }: { patientId: string; refs: string[]; label?: string }) {
  const [events, setEvents] = useState<ClinicalEvent[] | null>(null);
  const falas = allRefs.filter((r) => r.startsWith("Fala/")).map((r) => Number(r.split("/")[1]));
  const refs = allRefs.filter((r) => !r.startsWith("Fala/"));

  async function onOpen(open: boolean) {
    if (open && !events) {
      const all = await fetchTimeline(patientId);
      setEvents(all.filter((e) => refs.includes(e.source_ref)));
    }
  }

  if (refs.length === 0 && falas.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1">
      {falas.map((n) => (
        <Button
          key={n}
          variant="link"
          size="sm"
          className="min-h-9 px-1 text-xs"
          onClick={() => focusFala(n)}
          aria-label={`Ir para a fala ${n} da conversa`}
        >
          <MessageSquareQuote aria-hidden />
          fala {n}
        </Button>
      ))}
      {refs.length > 0 && <FhirRefs patientId={patientId} refs={refs} label={label} events={events} onOpen={onOpen} />}
    </span>
  );
}

function FhirRefs({
  patientId,
  refs,
  label,
  events,
  onOpen,
}: {
  patientId: string;
  refs: string[];
  label: string;
  events: ClinicalEvent[] | null;
  onOpen: (open: boolean) => Promise<void>;
}) {
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
              if (ref.startsWith("Checklist/")) {
                return (
                  <li key={ref} className="rounded-md border p-3 text-sm">
                    <p className="font-mono text-xs text-muted-foreground">{ref}</p>
                    <p className="mt-1">Resposta do checklist social de alta ({ref.split("/")[1].replaceAll("_", " ")}).</p>
                  </li>
                );
              }
              return (
                <li key={ref} className="rounded-md border p-3 text-sm">
                  <p className="font-mono text-xs text-muted-foreground">{ref}</p>
                  {matches.length === 0 && (
                    <p className="mt-1 text-muted-foreground">
                      {CONTEXT_LABEL[ref.split("/")[0]] ?? "Registro de contexto (diagnóstico, alergia ou prescrição ativa)."}
                    </p>
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
