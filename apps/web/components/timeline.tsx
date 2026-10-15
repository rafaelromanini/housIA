"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowRightLeft,
  ClipboardList,
  Droplets,
  FileText,
  FlaskConical,
  OctagonAlert,
  Pill,
  Stethoscope,
  Syringe,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { ClinicalEvent, EventKind } from "@/lib/api";
import { dayOf, fmtDay, fmtTime } from "@/lib/format";
import { useTimeline } from "@/lib/timeline-cache";
import { cn } from "@/lib/utils";

export const KIND_META: Record<EventKind, { label: string; Icon: LucideIcon }> = {
  vital: { label: "Sinais vitais/escalas", Icon: Activity },
  lab: { label: "Exames", Icon: FlaskConical },
  med_order: { label: "Prescrições", Icon: Pill },
  med_admin: { label: "Administrações", Icon: Syringe },
  note: { label: "Evoluções", Icon: FileText },
  procedure: { label: "Procedimentos", Icon: Stethoscope },
  transfer: { label: "Transferências", Icon: ArrowRightLeft },
  diagnosis: { label: "Diagnósticos", Icon: ClipboardList },
  io: { label: "Diurese/balanço", Icon: Droplets },
};

const ALL_KINDS = Object.keys(KIND_META) as EventKind[];

export function eventAnchor(ref: string) {
  return `ev-${ref.replace(/[^A-Za-z0-9-]/g, "_")}`;
}

export function Timeline({ patientId, highlightRef }: { patientId: string; highlightRef?: string | null }) {
  const { events, error } = useTimeline(patientId);
  const [kinds, setKinds] = useState<Set<EventKind>>(() => new Set(ALL_KINDS.filter((k) => k !== "med_admin")));
  const [onlyAbnormal, setOnlyAbnormal] = useState(false);

  const counts = useMemo(() => {
    const c: Partial<Record<EventKind, number>> = {};
    for (const e of events ?? []) c[e.kind] = (c[e.kind] ?? 0) + 1;
    return c;
  }, [events]);

  const visible = useMemo(() => {
    const list = (events ?? []).filter(
      (e) => (kinds.has(e.kind) || e.source_ref === highlightRef) && (!onlyAbnormal || (e.severity && e.severity !== "normal")),
    );
    return [...list].reverse();
  }, [events, kinds, onlyAbnormal, highlightRef]);

  const days = useMemo(() => {
    const map = new Map<string, ClinicalEvent[][]>();
    for (const e of visible) {
      const key = dayOf(e.timestamp);
      if (!map.has(key)) map.set(key, []);
      const groups = map.get(key)!;
      const last = groups[groups.length - 1];
      const head = last?.[0];
      const groupable = e.kind === "vital" || e.kind === "lab";
      if (groupable && head && head.kind === e.kind && head.timestamp === e.timestamp && head.encounter_id === e.encounter_id) {
        last.push(e);
      } else {
        groups.push([e]);
      }
    }
    return [...map.entries()];
  }, [visible]);

  useEffect(() => {
    if (!highlightRef || !events) return;
    const el = document.getElementById(eventAnchor(highlightRef));
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlightRef, events]);

  function toggle(kind: EventKind) {
    setKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }

  if (error) return <Alert variant="critico">Erro ao carregar a timeline: {error}</Alert>;
  if (!events) return <Skeleton className="h-96 w-full" />;

  return (
    <div className="space-y-3">
      <fieldset className="flex flex-wrap gap-2">
        <legend className="sr-only">Filtrar por tipo de evento</legend>
        {ALL_KINDS.map((k) => {
          const { label, Icon } = KIND_META[k];
          const on = kinds.has(k);
          return (
            <button
              key={k}
              type="button"
              aria-pressed={on}
              onClick={() => toggle(k)}
              className={cn(
                "inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                on ? "border-primary bg-primary text-primary-foreground" : "bg-card text-muted-foreground hover:bg-secondary",
              )}
            >
              <Icon aria-hidden className="size-3.5" />
              {label} <span className="tabular-nums opacity-80">({counts[k] ?? 0})</span>
            </button>
          );
        })}
        <button
          type="button"
          aria-pressed={onlyAbnormal}
          onClick={() => setOnlyAbnormal((v) => !v)}
          className={cn(
            "inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            onlyAbnormal ? "border-atencao bg-atencao-bg text-atencao" : "bg-card text-muted-foreground hover:bg-secondary",
          )}
        >
          <TriangleAlert aria-hidden className="size-3.5" />
          Só fora da faixa
        </button>
      </fieldset>

      {days.length === 0 && <p className="text-sm text-muted-foreground">Nenhum evento com os filtros selecionados.</p>}

      <ol className="space-y-5" aria-label="Timeline clínica (mais recentes primeiro)">
        {days.map(([day, groups]) => (
          <li key={day}>
            <h3 className="sticky top-14 z-10 mb-2 bg-background/95 py-1 text-sm font-bold text-muted-foreground first-letter:uppercase">
              {fmtDay(groups[0][0].timestamp)}
            </h3>
            <ol className="relative space-y-2 border-l-2 border-border pl-4">
              {groups.map((g) =>
                g.length === 1 ? (
                  <TimelineItem key={g[0].id} event={g[0]} highlighted={g[0].source_ref === highlightRef} />
                ) : (
                  <TimelineGroup key={g[0].id} events={g} highlightRef={highlightRef} />
                ),
              )}
            </ol>
          </li>
        ))}
      </ol>
    </div>
  );
}

const MEASURE_ORDER = ["pa", "fc", "fr", "temp", "spo2", "glicemia", "dor", "morse", "braden"];

function measureRank(e: ClinicalEvent) {
  const key = String(e.value?.chave ?? "");
  const i = MEASURE_ORDER.indexOf(key);
  return i === -1 ? MEASURE_ORDER.length : i;
}

function worstSeverity(events: ClinicalEvent[]) {
  if (events.some((e) => e.severity === "critico")) return "critico";
  if (events.some((e) => e.severity === "atencao")) return "atencao";
  return null;
}

/** Medidas do mesmo horário (ex.: PA, FC, FR, SpO2) numa linha só; cada valor mantém a própria fonte. */
function TimelineGroup({ events, highlightRef }: { events: ClinicalEvent[]; highlightRef?: string | null }) {
  const head = events[0];
  const { label, Icon } = KIND_META[head.kind];
  const sev = worstSeverity(events);
  const highlighted = events.some((e) => e.source_ref === highlightRef);
  return (
    <li
      className={cn(
        "relative rounded-md border bg-card p-2.5 text-sm",
        sev === "critico" && "border-critico-border",
        sev === "atencao" && "border-atencao-border",
        highlighted && "ring-2 ring-ring ring-offset-2",
      )}
    >
      <span aria-hidden className="absolute -left-[1.4rem] top-3 flex size-4 items-center justify-center rounded-full border-2 border-card bg-primary" />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <time dateTime={head.timestamp} className="font-bold tabular-nums">
          {fmtTime(head.timestamp)}
        </time>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Icon aria-hidden className="size-3.5" />
          {label}
        </span>
        {head.location && <span className="text-xs text-muted-foreground">· {head.location}</span>}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {[...events].sort((a, b) => measureRank(a) - measureRank(b)).map((e) => {
          const s = e.severity && e.severity !== "normal" ? e.severity : null;
          return (
            <li
              key={e.id}
              id={eventAnchor(e.source_ref)}
              title={`Fonte: ${e.source_ref}`}
              className={cn(
                "inline-flex items-center gap-1 rounded border px-2 py-0.5 tabular-nums",
                s === "critico" && "border-critico-border bg-critico-bg text-critico font-bold",
                s === "atencao" && "border-atencao-border bg-atencao-bg text-atencao font-bold",
                !s && "bg-muted/60",
                e.source_ref === highlightRef && "ring-2 ring-ring",
              )}
            >
              {s === "critico" && <OctagonAlert aria-label="Crítico" className="size-3.5" />}
              {s === "atencao" && <TriangleAlert aria-label="Fora da faixa" className="size-3.5" />}
              {e.summary.replace(/ \((fora da faixa|CRÍTICO)\)$/, "")}
            </li>
          );
        })}
      </ul>
    </li>
  );
}

function TimelineItem({ event: e, highlighted }: { event: ClinicalEvent; highlighted: boolean }) {
  const { label, Icon } = KIND_META[e.kind];
  const sev = e.severity && e.severity !== "normal" ? e.severity : null;
  return (
    <li
      id={eventAnchor(e.source_ref)}
      className={cn(
        "relative rounded-md border bg-card p-2.5 text-sm transition-shadow",
        sev === "critico" && "border-critico-border",
        sev === "atencao" && "border-atencao-border",
        highlighted && "ring-2 ring-ring ring-offset-2",
      )}
    >
      <span aria-hidden className="absolute -left-[1.4rem] top-3 flex size-4 items-center justify-center rounded-full border-2 border-card bg-primary" />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <time dateTime={e.timestamp} className="font-bold tabular-nums">
          {fmtTime(e.timestamp)}
        </time>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Icon aria-hidden className="size-3.5" />
          {label}
        </span>
        {e.location && <span className="text-xs text-muted-foreground">· {e.location}</span>}
        {sev === "critico" && (
          <Badge variant="critico">
            <OctagonAlert aria-hidden /> Crítico
          </Badge>
        )}
        {sev === "atencao" && (
          <Badge variant="atencao">
            <TriangleAlert aria-hidden /> Fora da faixa
          </Badge>
        )}
      </div>
      {e.kind === "note" && e.text ? (
        <details className="mt-1">
          <summary className="cursor-pointer">{e.summary}</summary>
          <p className="mt-2 whitespace-pre-wrap rounded bg-muted p-2">{e.text}</p>
        </details>
      ) : (
        <p className="mt-1">{e.summary}</p>
      )}
      <p className="mt-1 font-mono text-[11px] text-muted-foreground">Fonte: {e.source_ref}</p>
    </li>
  );
}
