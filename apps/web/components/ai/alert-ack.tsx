"use client";

import { useState } from "react";
import { Check } from "lucide-react";

import { Button } from "@/components/ui/button";
import { api, type AlertItem } from "@/lib/api";

export function AckButton({ alert, onChange }: { alert: AlertItem | undefined; onChange: (a: AlertItem) => void }) {
  const [busy, setBusy] = useState(false);
  if (!alert) return null;
  if (alert.status !== "aberto") {
    return (
      <span className="inline-flex min-h-9 items-center gap-1 text-xs font-bold text-ok">
        <Check aria-hidden className="size-4" />
        {alert.status === "reconhecido" ? "Reconhecido" : "Descartado"}
      </span>
    );
  }
  return (
    <Button
      size="sm"
      variant="outline"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          onChange(await api<AlertItem>(`/alerts/${alert.id}/status`, { method: "POST", json: { status: "reconhecido" } }));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Check aria-hidden />
      Reconhecer alerta
    </Button>
  );
}
