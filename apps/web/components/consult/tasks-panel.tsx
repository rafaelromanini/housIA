"use client";

import { useCallback, useEffect, useState } from "react";
import { ListChecks, Loader2, Plus } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, ApiError } from "@/lib/api";

type Papel = "medico" | "enfermagem" | "gestor";
type Tarefa = {
  id: number;
  titulo: string;
  papel: Papel;
  prazo: string;
  status: "aberta" | "concluida" | "cancelada";
  critica: boolean;
  fontes: string[];
  observacao_encerramento: string | null;
};
type Lista = { tarefas: Tarefa[]; abertas: number; criticas_abertas: number; aviso: string };

const PAPEL: Record<Papel, string> = { medico: "Médico(a)", enfermagem: "Enfermagem", gestor: "Gestão" };

function reason(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : "Não foi possível concluir";
}

function dia(iso: string): string {
  return new Date(iso).toLocaleDateString("pt-BR");
}

/** Pendências que a conversa gerou (retorno, pedido, ligação com o resultado, protocolo): responsável, prazo e fechamento. */
export function TasksPanel({ consultId, patientId }: { consultId: number; patientId: string }) {
  const base = `/consultations/${consultId}/tarefas`;
  const [lista, setLista] = useState<Lista | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fechando, setFechando] = useState<number | null>(null);
  const [nota, setNota] = useState("");
  const [novo, setNovo] = useState("");

  const refresh = useCallback(async () => {
    try {
      setLista(await api<Lista>(base));
    } catch {
      setLista(null);
    }
  }, [base]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(reason(e));
    } finally {
      setBusy(false);
    }
  }

  if (!lista) return null;
  const abertas = lista.tarefas.filter((t) => t.status === "aberta");

  return (
    <section className="space-y-3 rounded-lg border bg-card p-4" aria-labelledby="tarefas-titulo" data-testid="tarefas-consulta">
      <div className="flex flex-wrap items-center gap-2">
        <ListChecks className="size-4 text-primary" aria-hidden />
        <h3 id="tarefas-titulo" className="font-semibold">
          Tarefas desta consulta
        </h3>
        <Badge variant={lista.criticas_abertas ? "critico" : "outline"}>
          {lista.abertas} aberta(s){lista.criticas_abertas ? `, ${lista.criticas_abertas} crítica(s)` : ""}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">{lista.aviso}</p>
      {error && <Alert variant="critico">{error}</Alert>}
      {lista.tarefas.length === 0 && <p className="text-sm text-muted-foreground">Nenhuma pendência gerada pela conversa.</p>}
      <ul className="space-y-2">
        {lista.tarefas.map((t) => (
          <li key={t.id} className="rounded-md border p-3 text-sm" data-testid="tarefa">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{t.titulo}</span>
              {t.critica && <Badge variant="critico">Crítica</Badge>}
              {t.status !== "aberta" && <Badge variant="outline">{t.status === "concluida" ? "Concluída" : "Cancelada"}</Badge>}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Responsável: {PAPEL[t.papel]} · prazo {dia(t.prazo)}
            </p>
            <SourceRefs patientId={patientId} refs={t.fontes} />
            {t.status === "aberta" && (
              <div className="mt-2 flex flex-wrap items-end gap-2">
                <div className="space-y-1">
                  <Label htmlFor={`papel-${t.id}`}>Responsável</Label>
                  <NativeSelect
                    id={`papel-${t.id}`}
                    value={t.papel}
                    disabled={busy}
                    onChange={(e) =>
                      void run(() => api(`/journey/tasks/${t.id}`, { method: "PUT", json: { papel: e.target.value } }))
                    }
                  >
                    {(Object.keys(PAPEL) as Papel[]).map((p) => (
                      <option key={p} value={p}>
                        {PAPEL[p]}
                      </option>
                    ))}
                  </NativeSelect>
                </div>
                <div className="space-y-1">
                  <Label htmlFor={`prazo-${t.id}`}>Prazo</Label>
                  <Input
                    id={`prazo-${t.id}`}
                    type="date"
                    defaultValue={t.prazo.slice(0, 10)}
                    disabled={busy}
                    onBlur={(e) => {
                      if (e.target.value && e.target.value !== t.prazo.slice(0, 10))
                        void run(() =>
                          api(`/journey/tasks/${t.id}`, { method: "PUT", json: { prazo: `${e.target.value}T12:00:00-03:00` } }),
                        );
                    }}
                  />
                </div>
                <Button variant="outline" size="sm" disabled={busy} onClick={() => (setFechando(t.id), setNota(""))}>
                  Concluir
                </Button>
              </div>
            )}
            {fechando === t.id && (
              <div className="mt-2 space-y-1.5">
                <Label htmlFor={`nota-${t.id}`}>
                  {t.critica ? "O que foi feito (obrigatório, mín. 10 caracteres)" : "Observação (opcional)"}
                </Label>
                <Input id={`nota-${t.id}`} value={nota} onChange={(e) => setNota(e.target.value)} />
                <Button
                  size="sm"
                  disabled={busy || (t.critica && nota.trim().length < 10)}
                  onClick={() =>
                    void run(async () => {
                      await api(`/journey/tasks/${t.id}`, { method: "PATCH", json: { status: "concluida", observacao: nota } });
                      setFechando(null);
                    })
                  }
                >
                  {busy && <Loader2 className="animate-spin" aria-hidden />}
                  Confirmar conclusão
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {abertas.length > 0 && <p className="text-xs text-muted-foreground">As tarefas abertas aparecem na passagem de plantão e na alta.</p>}
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-60 flex-1 space-y-1">
          <Label htmlFor="tarefa-nova">Nova pendência</Label>
          <Input id="tarefa-nova" value={novo} onChange={(e) => setNovo(e.target.value)} placeholder="Ex.: pedir o laudo do ecocardiograma" />
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || novo.trim().length < 3}
          onClick={() =>
            void run(async () => {
              await api(base, { method: "POST", json: { titulo: novo.trim() } });
              setNovo("");
            })
          }
        >
          <Plus aria-hidden /> Adicionar
        </Button>
      </div>
    </section>
  );
}
