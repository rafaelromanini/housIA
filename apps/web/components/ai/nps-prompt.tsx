"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";

/** NPS de uma pergunta, uma vez por mês e por profissional ("de 0 a 10, o quanto recomendaria o HousIA?"). */
export function NpsPrompt() {
  const state = useApi<{ mes: string; respondido: boolean; nota: number | null }>("/feedback/nps");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!state.data || state.data.respondido) {
    return state.data ? <p className="text-xs text-muted-foreground">Obrigado pela nota deste mês: {state.data.nota}.</p> : null;
  }

  async function send(score: number) {
    setBusy(true);
    setError(null);
    try {
      await api("/feedback", { method: "POST", json: { kind: "nps", score } });
      await state.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível registrar.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <fieldset className="space-y-2 rounded-lg border bg-card p-3" disabled={busy}>
      <legend className="px-1 text-sm font-bold">De 0 a 10, o quanto você recomendaria o HousIA a um colega?</legend>
      <div className="flex flex-wrap gap-1.5">
        {Array.from({ length: 11 }, (_, n) => (
          <Button key={n} type="button" variant="outline" className="size-11 p-0" aria-label={`Nota ${n}`} onClick={() => void send(n)}>
            {n}
          </Button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">Uma pergunta por mês. 0 = de forma nenhuma, 10 = com certeza.</p>
      {error && <p role="alert" className="text-xs text-critico">{error}</p>}
    </fieldset>
  );
}
