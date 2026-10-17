"use client";

import { useId, useState } from "react";
import { Check, Pin, PinOff, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { InsightAction, InsightItem } from "@/lib/api";

/** O selo "novo" dura 2 falas (igual ao NEW_FOR do backend). */
const NEW_FOR = 2;
const MIN_MOTIVO = 10;

export type OnInsightAction = (itemKey: string, acao: InsightAction, motivo?: string) => Promise<void>;

export function isNewItem(item: InsightItem, falas: number) {
  return (item.primeira_fala ?? 0) > 0 && falas - (item.primeira_fala ?? 0) < NEW_FOR;
}

export function NewBadge({ item, falas }: { item: InsightItem; falas: number }) {
  if (!isNewItem(item, falas)) return null;
  return (
    <Badge variant="info" className="text-[11px]" data-testid="selo-novo">
      novo
    </Badge>
  );
}

const LABEL: Record<Exclude<InsightAction, "fixar">, string> = {
  aceitar: "Aceitar",
  feita: "Feita",
  nao_se_aplica: "Não se aplica",
  descartar: "Descartar",
};

/** Botões de decisão de um insight: aceitar / feita / não se aplica / descartar (com motivo) / fixar. */
export function ItemActions({
  item,
  label,
  actions,
  onAction,
}: {
  item: InsightItem;
  /** texto do item, para o nome acessível dos botões */
  label: string;
  actions: Exclude<InsightAction, "fixar">[];
  onAction?: OnInsightAction;
}) {
  const [discarding, setDiscarding] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const key = item.item_key;
  if (!onAction || !key) return null;
  const short = label.length > 60 ? `${label.slice(0, 60)}…` : label;

  async function run(acao: InsightAction, motivo?: string) {
    setBusy(true);
    try {
      await onAction!(key!, acao, motivo);
      setDiscarding(false);
      setReason("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-1.5">
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label={`Decisão sobre: ${short}`}>
        {actions.map((a) => (
          <Button
            key={a}
            variant={a === "descartar" || a === "nao_se_aplica" ? "outline" : "secondary"}
            size="sm"
            className="min-h-9 px-2 text-xs"
            disabled={busy}
            aria-pressed={item.acao === a}
            aria-label={`${LABEL[a]}: ${short}`}
            onClick={() => (a === "descartar" ? setDiscarding((v) => !v) : void run(a))}
          >
            {a === "aceitar" || a === "feita" ? <Check aria-hidden /> : a === "descartar" ? <X aria-hidden /> : null}
            {LABEL[a]}
          </Button>
        ))}
        <Button
          variant="ghost"
          size="sm"
          className="min-h-9 px-2 text-xs"
          disabled={busy}
          aria-pressed={!!item.fixado}
          aria-label={`${item.fixado ? "Soltar" : "Fixar no topo"}: ${short}`}
          onClick={() => void run("fixar")}
        >
          {item.fixado ? <PinOff aria-hidden /> : <Pin aria-hidden />}
          {item.fixado ? "Soltar" : "Fixar"}
        </Button>
        {item.acao === "aceitar" && <Badge variant="ok" className="text-[11px]">aceito</Badge>}
        {item.acao === "feita" && <Badge variant="ok" className="text-[11px]">feita</Badge>}
      </div>
      {discarding && (
        <form
          className="mt-1 flex flex-wrap items-end gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (reason.trim().length >= MIN_MOTIVO) void run("descartar", reason.trim());
          }}
        >
          <div className="min-w-48 flex-1">
            <label htmlFor={id} className="block text-xs font-bold">
              Motivo do descarte (mín. {MIN_MOTIVO} caracteres)
            </label>
            <Input id={id} value={reason} onChange={(e) => setReason(e.target.value)} className="h-9" />
          </div>
          <Button type="submit" size="sm" className="min-h-9 text-xs" disabled={busy || reason.trim().length < MIN_MOTIVO}>
            Confirmar descarte
          </Button>
        </form>
      )}
    </div>
  );
}
