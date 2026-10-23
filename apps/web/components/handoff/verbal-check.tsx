"use client";

import { useState } from "react";
import { Loader2, MessageSquareText, Mic, ScanSearch, ShieldAlert } from "lucide-react";

import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ResultadoConferencia } from "@/components/handoff/resultado-conferencia";
import { VerbalLive } from "@/components/handoff/verbal-live";
import { api, type Audience, type HandoffCheck } from "@/lib/api";

export function VerbalCheck({ audience, windowH }: { audience: Audience; windowH: number }) {
  const { patient } = usePatient();
  const [text, setText] = useState("");
  const [result, setResult] = useState<HandoffCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modo, setModo] = useState<"texto" | "voz">("texto");

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (text.trim().length < 10) {
      setError("Escreva o relato da passagem (mín. 10 caracteres).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api<HandoffCheck>(`/patients/${patient.id}/handoff/check`, {
          method: "POST",
          json: { audience, texto: text, window_hours: windowH },
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro na conferência");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquareText aria-hidden className="size-5 text-primary" /> Conferir a passagem verbal
        </CardTitle>
        <CardDescription>
          Digite ou cole o que foi passado verbalmente. O HousIA compara com o prontuário e mostra o que ficou de fora ou
          contradiz os registros.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-2" role="group" aria-label="Como conferir a passagem">
          <Button type="button" variant={modo === "texto" ? "default" : "outline"} aria-pressed={modo === "texto"} onClick={() => setModo("texto")}>
            Digitar ou colar
          </Button>
          <Button type="button" variant={modo === "voz" ? "default" : "outline"} aria-pressed={modo === "voz"} onClick={() => setModo("voz")}>
            <Mic aria-hidden /> Falar ao vivo
          </Button>
        </div>
        {modo === "voz" ? (
          <VerbalLive audience={audience} windowH={windowH} />
        ) : (
          <>
        <form onSubmit={run} className="space-y-2" noValidate>
          <Label htmlFor="relato">Relato de quem está saindo</Label>
          <Textarea
            id="relato"
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setError(null);
            }}
            className="min-h-28"
            placeholder="Ex.: Paciente veio da UTI, pós-op de colecistectomia, PA estável, dreno em flanco direito, ceftriaxona às 20h…"
            aria-invalid={error ? true : undefined}
            aria-describedby="relato-ajuda"
          />
          <p id="relato-ajuda" className="text-xs text-muted-foreground">
            O texto é pseudonimizado antes de ir para a IA e não fica gravado na auditoria (só as contagens).
          </p>
          <Button type="submit" disabled={busy}>
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <ScanSearch aria-hidden />}
            {busy ? "Conferindo…" : "Conferir com o prontuário"}
          </Button>
        </form>
          </>
        )}
        {modo === "texto" && error && (
          <Alert variant="critico">
            <ShieldAlert aria-hidden />
            {error}
          </Alert>
        )}
        {modo === "texto" && result && <ResultadoConferencia result={result} />}
      </CardContent>
    </Card>
  );
}
