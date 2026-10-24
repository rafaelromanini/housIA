"use client";

import { useRef, useState } from "react";
import { FileUp, Loader2, Trash2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { api, ApiError, type Consultation, type Laudo } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const MAX_MB = 8;

function reason(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return String((detail as { mensagem: unknown }).mensagem);
  }
  return e instanceof Error ? e.message : "Não foi possível ler o laudo";
}

/** Laudos em PDF ou foto: os valores viram exames com a página de origem. O arquivo é descartado depois da leitura. */
export function LaudosPanel({
  consult,
  editable,
  onChange,
}: {
  consult: Consultation;
  editable: boolean;
  onChange: (next: Consultation) => void;
}) {
  const { user } = useAuth();
  const input = useRef<HTMLInputElement>(null);
  const [ciente, setCiente] = useState(false);
  const [guardar, setGuardar] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const laudos: Laudo[] = consult.laudos ?? [];
  const base = `/patients/${consult.patient_id}/documents`;

  async function reload() {
    onChange(await api<Consultation>(`/consultations/${consult.id}`));
  }

  async function send(file: File) {
    setError(null);
    if (file.size > MAX_MB * 1024 * 1024) {
      setError(`O arquivo passa de ${MAX_MB} MB.`);
      return;
    }
    setBusy(true);
    try {
      await api(`${base}?consulta=${consult.id}&ciente=${ciente}&guardar=${guardar}`, {
        method: "POST",
        body: file,
        headers: { "Content-Type": file.type || "application/octet-stream" },
      });
      await reload();
    } catch (e) {
      setError(reason(e));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  async function remove(n: number) {
    setBusy(true);
    setError(null);
    try {
      await api(`${base}/${n}?consulta=${consult.id}`, { method: "DELETE" });
      await reload();
    } catch (e) {
      setError(reason(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="laudos-titulo" data-testid="laudos-consulta" className="space-y-2 rounded-md border p-3">
      <h3 id="laudos-titulo" className="flex flex-wrap items-center gap-2 text-sm font-bold">
        <FileUp aria-hidden className="size-4 text-primary" /> Laudos em PDF ou foto
        <Badge variant="outline">{laudos.length} enviado(s)</Badge>
        {busy && <Loader2 aria-hidden className="size-4 animate-spin" />}
      </h3>
      <p className="text-xs text-muted-foreground">
        Os valores lidos viram exames com a página de origem e entram nas propostas para o prontuário, que você aceita item a item. O arquivo é
        lido no servidor do HousIA e descartado em seguida (PDF ou foto PNG/JPEG, até {MAX_MB} MB).
      </p>
      {error && <Alert variant="critico">{error}</Alert>}
      {editable && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Checkbox id="laudo-ciente" checked={ciente} onCheckedChange={(v) => setCiente(v === true)} />
            <Label htmlFor="laudo-ciente">Confirmo que este laudo é deste paciente</Label>
          </div>
          {user?.role === "medico" && (
            <div className="flex items-center gap-2">
              <Checkbox id="laudo-guardar" checked={guardar} onCheckedChange={(v) => setGuardar(v === true)} />
              <Label htmlFor="laudo-guardar">Guardar o arquivo no prontuário (padrão: descartar)</Label>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={input}
              id="laudo-arquivo"
              type="file"
              accept="application/pdf,image/png,image/jpeg"
              className="sr-only"
              disabled={busy || !ciente}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void send(f);
              }}
            />
            <Button type="button" variant="outline" disabled={busy || !ciente} onClick={() => input.current?.click()}>
              <FileUp aria-hidden /> Enviar laudo
            </Button>
            {!ciente && <span className="text-xs text-muted-foreground">Marque a confirmação para enviar.</span>}
          </div>
        </div>
      )}
      <ul className="space-y-2">
        {laudos.map((l) => (
          <li key={l.n} className="rounded-md border p-2 text-sm" data-testid="laudo">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{l.ref}</span>
              <Badge variant="outline">
                {l.tipo === "pdf" ? "PDF" : "Foto"}, {l.paginas} pág.
              </Badge>
              {l.data_laudo && (
                <span className="text-xs text-muted-foreground">
                  coleta {new Date(l.data_laudo).toLocaleDateString("pt-BR", { timeZone: "UTC" })}
                </span>
              )}
              {l.guardado && <Badge variant="outline">Arquivo guardado no prontuário</Badge>}
              {editable && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => void remove(l.n)}
                  aria-label={`Descartar os valores do ${l.ref}`}
                >
                  <Trash2 aria-hidden /> Descartar
                </Button>
              )}
            </div>
            {l.avisos.map((a) => (
              <p key={a} className="text-xs text-muted-foreground">
                {a}
              </p>
            ))}
            <ul className="mt-1 space-y-0.5">
              {l.valores.map((v) => (
                <li key={`${v.chave}-${v.pagina}`} className="text-xs">
                  <span className="font-medium">{v.exame}</span> {String(v.valor).replace(".", ",")} {v.unidade} · p. {v.pagina}
                  {v.comparacao ? ` · ${v.comparacao}` : ""}
                </li>
              ))}
            </ul>
            {l.ignorados.length > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                Não lidos: {l.ignorados.map((i) => `${i.exame} (${i.motivo})`).join("; ")}.
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
