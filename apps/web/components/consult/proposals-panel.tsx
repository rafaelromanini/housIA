"use client";

import { useState } from "react";
import { ClipboardList, Loader2 } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { api, ApiError, type Proposal } from "@/lib/api";

/** Painel "Propostas para o prontuário": o profissional marca, item a item, o que vira dado estruturado no FHIR. */
export function ProposalsPanel({
  consultId,
  patientId,
  proposals,
  editable,
  onChange,
}: {
  consultId: number;
  patientId: string;
  proposals: Proposal[];
  editable: boolean;
  onChange: (next: Proposal[]) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle(id: string, checked: boolean) {
    const accepted = proposals.filter((p) => (p.id === id ? checked : p.aceita)).map((p) => p.id);
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ propostas: Proposal[] }>(`/consultations/${consultId}/proposals`, { method: "PUT", json: { aceitas: accepted } });
      onChange(out.propostas);
    } catch (e) {
      const detail = e instanceof ApiError ? (e.body as { detail?: unknown } | null)?.detail : null;
      setError(typeof detail === "string" ? detail : e instanceof Error ? e.message : "Não foi possível salvar a decisão");
    } finally {
      setBusy(false);
    }
  }

  const accepted = proposals.filter((p) => p.aceita).length;
  return (
    <section aria-labelledby="propostas-prontuario" data-testid="propostas-prontuario" className="space-y-2 rounded-md border p-3">
      <h3 id="propostas-prontuario" className="flex flex-wrap items-center gap-2 text-sm font-bold">
        <ClipboardList aria-hidden className="size-4 text-primary" /> Propostas para o prontuário
        <Badge variant="outline">{accepted} de {proposals.length} aceita{accepted === 1 ? "" : "s"}</Badge>
        {busy && <Loader2 aria-hidden className="size-4 animate-spin" />}
      </h3>
      <p className="text-xs text-muted-foreground">
        O que a conversa trouxe e pode entrar como dado estruturado (cada um com a fala de origem no Provenance). Nada vai sem o seu aceite.
      </p>
      {error && <Alert variant="critico">{error}</Alert>}
      <ul className="space-y-1.5">
        {proposals.map((p) => {
          const inputId = `proposta-${p.id}`;
          return (
            <li key={p.id} className="flex items-start gap-2 rounded-md bg-secondary/40 p-2">
              <Checkbox
                id={inputId}
                checked={p.aceita}
                disabled={!editable || busy}
                onCheckedChange={(v) => void toggle(p.id, v === true)}
                className="mt-0.5"
              />
              <div className="min-w-0 flex-1">
                <Label htmlFor={inputId} className="cursor-pointer flex-wrap leading-snug">
                  <Badge variant="outline" className="text-[11px]">{p.tipo_rotulo}</Badge>
                  <span className="font-bold">{p.rotulo}</span>
                </Label>
                <p className="break-words text-xs text-muted-foreground">{p.detalhe}</p>
                <SourceRefs patientId={patientId} refs={p.refs} />
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
