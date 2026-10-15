"use client";

import { useEffect, useState } from "react";

import { api, type ClinicalEvent } from "@/lib/api";

const cache = new Map<string, Promise<ClinicalEvent[]>>();

export function fetchTimeline(patientId: string, force = false): Promise<ClinicalEvent[]> {
  if (force || !cache.has(patientId)) {
    const p = api<ClinicalEvent[]>(`/patients/${encodeURIComponent(patientId)}/timeline`);
    p.catch(() => cache.delete(patientId));
    cache.set(patientId, p);
  }
  return cache.get(patientId)!;
}

export function useTimeline(patientId: string) {
  const [events, setEvents] = useState<ClinicalEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    fetchTimeline(patientId)
      .then((ev) => alive && setEvents(ev))
      .catch((e) => alive && setError(e instanceof Error ? e.message : "Erro ao carregar timeline"));
    return () => {
      alive = false;
    };
  }, [patientId]);
  return { events, error };
}
