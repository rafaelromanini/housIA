"use client";

import { useState } from "react";
import { Check, ThumbsDown, ThumbsUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, type FeedbackKind } from "@/lib/api";

/** "Isto foi útil?" — mede a qualidade das saídas da IA durante o piloto. */
export function FeedbackButtons({ kind, entityId, patientId }: { kind: FeedbackKind; entityId?: string | number; patientId: string }) {
  const [state, setState] = useState<"idle" | "comment" | "sent">("idle");
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function send(useful: boolean) {
    setError(null);
    try {
      await api("/feedback", {
        method: "POST",
        json: { kind, entity_id: entityId != null ? String(entityId) : null, patient_id: patientId, useful, comment: comment || null },
      });
      setState("sent");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao enviar");
    }
  }

  if (state === "sent") {
    return (
      <p className="inline-flex items-center gap-1 text-xs font-bold text-ok" role="status">
        <Check aria-hidden className="size-4" /> Obrigado pelo retorno
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span>Isto foi útil?</span>
      <Button variant="outline" size="sm" aria-label="Sim, foi útil" onClick={() => void send(true)}>
        <ThumbsUp aria-hidden /> Sim
      </Button>
      <Button variant="outline" size="sm" aria-label="Não foi útil" onClick={() => setState("comment")}>
        <ThumbsDown aria-hidden /> Não
      </Button>
      {state === "comment" && (
        <form
          className="flex w-full flex-wrap items-center gap-2 sm:w-auto"
          onSubmit={(e) => {
            e.preventDefault();
            void send(false);
          }}
        >
          <label htmlFor={`fb-${kind}-${entityId}`} className="sr-only">
            O que não ajudou?
          </label>
          <Input
            id={`fb-${kind}-${entityId}`}
            className="min-h-9 w-64 text-sm"
            placeholder="O que não ajudou?"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
          <Button type="submit" size="sm">
            Enviar
          </Button>
        </form>
      )}
      {error && <span className="font-bold text-destructive">{error}</span>}
    </div>
  );
}
