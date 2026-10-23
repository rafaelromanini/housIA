"use client";

import { useState } from "react";
import { Loader2, Wand2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";

type Preview = {
  antes: string;
  depois: string;
  refs: string[];
  modo: string;
  aplicada: boolean;
  avisos: string[];
  diff: { pct_editado: number; palavras_antes: number; palavras_depois: number; operacoes: { op: string; antes: string; depois: string }[] };
  descartadas: { texto: string; motivo: string }[];
  protegidas: number;
  nota?: Record<string, unknown>;
};

function reason(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) {
      const d = detail as { mensagem: string; itens?: { texto: string }[] };
      return d.mensagem + (d.itens?.length ? ` Itens: ${d.itens.map((i) => i.texto).join("; ")}` : "");
    }
  }
  return e instanceof Error ? e.message : "Não foi possível reescrever";
}

/** "Reescrever por instrução" de uma seção: mostra a prévia com o diff e só grava quando o profissional aplica. */
export function SectionRewrite({
  consultId,
  sectionKey,
  disabled,
  onApplied,
}: {
  consultId: number;
  sectionKey: string;
  disabled: boolean;
  onApplied: (nota: Record<string, unknown>) => void;
}) {
  const [instrucao, setInstrucao] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const url = `/consultations/${consultId}/note/sections/${sectionKey}/rewrite`;

  async function run(aplicar: boolean) {
    setBusy(true);
    setError(null);
    try {
      const out = await api<Preview>(url, { method: "POST", json: { instrucao, aplicar } });
      if (aplicar && out.nota) {
        setPreview(null);
        setInstrucao("");
        onApplied(out.nota);
      } else {
        setPreview(out);
      }
    } catch (e) {
      setPreview(null);
      setError(reason(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1.5 rounded-md border border-dashed p-2" data-testid={`reescrever-${sectionKey}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Wand2 aria-hidden className="size-4 text-primary" />
        <Input
          aria-label="Instrução para reescrever a seção"
          placeholder="Ex.: resuma, sem siglas, acrescente: retorno em 30 dias"
          value={instrucao}
          maxLength={500}
          disabled={disabled || busy}
          onChange={(e) => setInstrucao(e.target.value)}
          className="h-8 min-w-48 flex-1"
        />
        <Button type="button" size="sm" variant="outline" disabled={disabled || busy || instrucao.trim().length < 3} onClick={() => void run(false)}>
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : null} Ver prévia
        </Button>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {preview && (
        <div className="space-y-1.5 text-sm" data-testid={`previa-${sectionKey}`}>
          <p className="text-xs text-muted-foreground">
            {preview.diff.palavras_antes} → {preview.diff.palavras_depois} palavras ({preview.diff.pct_editado}% alterado) ·{" "}
            {preview.protegidas} item(ns) de segurança mantido(s) · {preview.refs.length} fonte(s)
          </p>
          {preview.avisos.map((a) => (
            <Alert key={a} variant="atencao">{a}</Alert>
          ))}
          <div className="grid gap-2 sm:grid-cols-2">
            <div>
              <Badge variant="outline">antes</Badge>
              <p className="whitespace-pre-wrap break-words rounded bg-critico-bg/40 p-1.5">{preview.antes}</p>
            </div>
            <div>
              <Badge variant="outline">depois</Badge>
              <p className="whitespace-pre-wrap break-words rounded bg-ok-bg/40 p-1.5">{preview.depois}</p>
            </div>
          </div>
          {preview.descartadas.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Ficou de fora por não ter fonte: {preview.descartadas.map((d) => d.texto).join("; ")}.
            </p>
          )}
          <Button type="button" size="sm" disabled={busy || preview.antes === preview.depois} onClick={() => void run(true)}>
            Aplicar na nota (volta para a revisão)
          </Button>
        </div>
      )}
    </div>
  );
}
