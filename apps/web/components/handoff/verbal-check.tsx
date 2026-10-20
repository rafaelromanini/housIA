"use client";

import { useState } from "react";
import { CircleCheck, Loader2, MessageSquareText, ScanSearch, ShieldAlert, TriangleAlert, XCircle } from "lucide-react";

import { FeedbackButtons } from "@/components/ai/feedback";
import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityBadge } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type Audience, type HandoffCheck, type HandoffCheckItem } from "@/lib/api";
import { cn } from "@/lib/utils";

const GROUPS: { status: HandoffCheckItem["status"]; title: string; Icon: typeof XCircle; tone: string }[] = [
  { status: "divergente", title: "Divergências com o prontuário", Icon: TriangleAlert, tone: "text-critico" },
  { status: "omitido", title: "Não mencionado no relato", Icon: XCircle, tone: "text-atencao" },
  { status: "mencionado", title: "Mencionado", Icon: CircleCheck, tone: "text-ok" },
];

export function VerbalCheck({ audience, windowH }: { audience: Audience; windowH: number }) {
  const { patient } = usePatient();
  const [text, setText] = useState("");
  const [result, setResult] = useState<HandoffCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        {error && (
          <Alert variant="critico">
            <ShieldAlert aria-hidden />
            {error}
          </Alert>
        )}
        {result && (
          <div className="space-y-3" aria-live="polite">
            <div className="flex flex-wrap items-center gap-2">
              <AiBadge />
              {result.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
              <Badge variant={result.contagem.divergente ? "critico" : result.contagem.omitido ? "atencao" : "ok"}>
                Cobertura {result.cobertura != null ? `${Math.round(result.cobertura * 100)}%` : "—"}
              </Badge>
            </div>
            <p className="font-bold">{reidentify(result.resumo, patient.nome)}</p>
            {GROUPS.map(({ status, title, Icon, tone }) => {
              const items = result.itens.filter((i) => i.status === status);
              if (items.length === 0) return null;
              return (
                <section key={status} aria-label={title}>
                  <h3 className={cn("mb-1.5 flex items-center gap-1.5 text-sm font-bold", tone)}>
                    <Icon aria-hidden className="size-4" /> {title} ({items.length})
                  </h3>
                  <ul className="space-y-1.5">
                    {items.map((it, i) => (
                      <li
                        key={i}
                        className={cn(
                          "rounded-md border p-2.5 text-sm",
                          status === "divergente" && "border-critico-border bg-critico-bg/60",
                          status === "omitido" && "border-atencao-border bg-atencao-bg/60",
                        )}
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <SeverityBadge severity={it.severidade} />
                          <span className="font-bold">{reidentify(it.fato, patient.nome)}</span>
                        </div>
                        <p className="mt-1">{reidentify(it.comentario, patient.nome)}</p>
                        <SourceRefs patientId={patient.id} refs={it.refs} />
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
            <FeedbackButtons kind="handoff_check" patientId={patient.id} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
