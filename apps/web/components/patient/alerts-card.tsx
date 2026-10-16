"use client";

import { useState } from "react";
import { BellRing, X } from "lucide-react";

import { AckButton } from "@/components/ai/alert-ack";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type AlertItem } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

const SOURCE = { handoff: "Passagem de plantão", medrec: "Reconciliação", discharge: "Alta", jornada: "Jornada pós-alta" } as const;
const ORDER = { critico: 0, atencao: 1, info: 2 } as const;

export function AlertsCard() {
  const { patient, reload } = usePatient();
  const { data, setData } = useApi<AlertItem[]>(`/patients/${patient.id}/alerts?status=aberto`);
  const items = [...(data ?? [])].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);

  function updated(a: AlertItem) {
    setData((prev) => (prev ?? []).filter((x) => x.id !== a.id));
    void reload();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BellRing aria-hidden className="size-4 text-primary" /> Alertas abertos ({items.length})
        </CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nenhum alerta aberto.</p>
        ) : (
          <ul className="space-y-2">
            {items.map((a) => (
              <li key={a.id} className="rounded-md border p-2.5 text-sm">
                <div className="flex gap-2">
                  <SeverityIcon severity={a.severity} className="mt-0.5 size-4 shrink-0" />
                  <div className="min-w-0 space-y-1">
                    <p className="font-bold leading-snug">{a.title.replaceAll("[PACIENTE]", patient.nome)}</p>
                    <p className="text-xs text-muted-foreground">
                      {SOURCE[a.source]} · {fmtDateTime(a.created_at)}
                    </p>
                    <div className="flex flex-wrap items-center gap-1">
                      <SourceRefs patientId={patient.id} refs={a.source_refs} />
                      <AckButton alert={a} onChange={updated} />
                      <DiscardDialog alert={a} onDone={updated} />
                    </div>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function DiscardDialog({ alert, onDone }: { alert: AlertItem; onDone: (a: AlertItem) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    if (text.trim().length < 10) {
      setError("Descreva o motivo (mín. 10 caracteres).");
      return;
    }
    try {
      onDone(await api<AlertItem>(`/alerts/${alert.id}/status`, { method: "POST", json: { status: "descartado", justificativa: text } }));
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao descartar");
    }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm">
          <X aria-hidden /> Descartar
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Descartar alerta</DialogTitle>
          <DialogDescription>{alert.title}</DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor={`desc-${alert.id}`}>Justificativa (fica registrada na auditoria)</Label>
          <Textarea
            id={`desc-${alert.id}`}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setError(null);
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `desc-err-${alert.id}` : undefined}
          />
          {error && (
            <p id={`desc-err-${alert.id}`} className="text-xs font-bold text-destructive">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancelar
          </Button>
          <Button variant="destructive" onClick={() => void submit()}>
            Descartar alerta
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
