"use client";

import { useRef, useState } from "react";
import { Loader2, Mic, MicOff } from "lucide-react";

import { speechSupported, useSpeechCapture } from "@/components/consult/capture";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { applyDictation } from "@/lib/voice-commands";

/** Botão de microfone do ditado (#41): liga e desliga o reconhecimento de fala do navegador e entrega cada trecho
 * final em `onText`. Sem reconhecimento no navegador o botão avisa e o campo segue digitável. */
export function DictateButton({
  label,
  disabled,
  onText,
}: {
  label: string;
  disabled?: boolean;
  onText: (text: string) => void;
}) {
  const { listening, interim, error, start, stop } = useSpeechCapture("pt-BR", onText);
  const supported = speechSupported();
  return (
    <span className="inline-flex flex-col gap-0.5">
      <Button
        type="button"
        size="sm"
        variant={listening ? "destructive" : "outline"}
        aria-pressed={listening}
        aria-label={listening ? `Parar o ditado: ${label}` : `Ditar: ${label}`}
        disabled={disabled}
        onClick={() => (listening ? void stop() : start())}
        data-testid="ditado-botao"
      >
        {listening ? <MicOff aria-hidden /> : <Mic aria-hidden />}
        {listening ? "Parar" : "Ditar"}
      </Button>
      {listening && interim && (
        <span className="max-w-64 truncate text-xs text-muted-foreground" aria-live="polite">
          {interim}
        </span>
      )}
      {error && <span className="max-w-64 text-xs text-critico">{error}</span>}
      {!supported && !error && <span className="max-w-64 text-xs text-muted-foreground">Sem reconhecimento de fala neste navegador: digite.</span>}
    </span>
  );
}

/** Ditado para um campo de texto livre (justificativa, override): comandos de voz aplicados no próprio campo. */
export function DictateText({
  label,
  value,
  onChange,
  disabled,
  onNext,
}: {
  label: string;
  value: string;
  onChange: (text: string) => void;
  disabled?: boolean;
  onNext?: () => void;
}) {
  const state = useRef({ text: value, history: [] as string[], newLine: false });
  state.current.text = value; // a digitação à mão vale: o ditado continua do que está na tela
  return (
    <DictateButton
      label={label}
      disabled={disabled}
      onText={(spoken) => {
        const out = applyDictation(state.current, spoken);
        state.current = out.state;
        if (out.next) onNext?.();
        else onChange(out.state.text);
      }}
    />
  );
}

export type DictationResult = {
  secao: string;
  nota: Record<string, unknown>;
  proximo_campo: boolean;
  comandos: { comando: string; efeito: string; motivo?: string }[];
};

function reason(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return (detail as { mensagem: string }).mensagem;
  }
  return e instanceof Error ? e.message : "Não foi possível gravar o ditado";
}

/** Ditado direto numa seção da nota, também depois de finalizar: cada trecho vai ao servidor como adendo (Fala/N). */
export function DictateSection({
  consultId,
  sectionKey,
  label,
  disabled,
  onApplied,
  onNext,
}: {
  consultId: number;
  sectionKey: string;
  label: string;
  disabled?: boolean;
  onApplied: (nota: Record<string, unknown>) => void;
  onNext?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const queue = useRef<Promise<void>>(Promise.resolve());

  function send(spoken: string) {
    // uma chamada por vez e na ordem em que foi dito: "desfazer" depende do que veio antes
    queue.current = queue.current.then(async () => {
      setBusy(true);
      setError(null);
      try {
        const out = await api<DictationResult>(`/consultations/${consultId}/addendum`, {
          method: "POST",
          json: { secao: sectionKey, segmentos: [{ texto: spoken, client_id: `d-${crypto.randomUUID()}` }], via: "navegador" },
        });
        onApplied(out.nota);
        const refused = out.comandos.find((c) => c.efeito !== "aplicado" && c.motivo);
        setNotice(refused?.motivo ?? null);
        if (out.proximo_campo) onNext?.();
      } catch (e) {
        setError(reason(e));
      } finally {
        setBusy(false);
      }
    });
  }

  return (
    <div className="flex flex-wrap items-start gap-2" data-testid={`ditado-${sectionKey}`}>
      <DictateButton label={label} disabled={disabled || busy} onText={send} />
      {busy && <Loader2 aria-hidden className="mt-2 size-4 animate-spin" />}
      {notice && <p className="text-xs text-muted-foreground">{notice}</p>}
      {error && <Alert variant="critico">{error}</Alert>}
    </div>
  );
}
