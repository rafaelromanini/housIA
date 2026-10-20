"use client";

import Link from "next/link";

import { Alert } from "@/components/ui/alert";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { AuditEntry, DrillItems, GestaoLine } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

/** Drill-down de um número: a fórmula e as linhas (audit_log ou pacientes/recursos FHIR) que o compõem. */
export function DrillDialog({
  path,
  kind,
  onClose,
}: {
  path: string | null;
  kind: "audit" | "gestao";
  onClose: () => void;
}) {
  const { data, error } = useApi<DrillItems<AuditEntry | GestaoLine>>(path);
  return (
    <Dialog open={path !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{data?.rotulo ?? "Linhas do número"}</DialogTitle>
          <DialogDescription>
            {data ? (
              <>
                <span className="block">
                  <strong>Fórmula:</strong> {data.formula}
                </span>
                <span className="block">
                  {data.total} linha(s) compõem o número ({data.valor ?? "—"} {data.unidade}).
                </span>
              </>
            ) : (
              "Carregando as linhas…"
            )}
          </DialogDescription>
        </DialogHeader>
        {error && <Alert variant="critico">{error}</Alert>}
        {!data && !error && <Skeleton className="h-40" />}
        {data && data.items.length === 0 && <p className="text-sm text-muted-foreground">Nenhuma linha no período.</p>}
        {data && data.items.length > 0 && kind === "audit" && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm">
              <caption className="sr-only">Linhas do audit_log que compõem o número</caption>
              <thead>
                <tr className="border-b text-left">
                  <th scope="col" className="py-2 pr-3">Quando</th>
                  <th scope="col" className="py-2 pr-3">Quem</th>
                  <th scope="col" className="py-2">Ação</th>
                </tr>
              </thead>
              <tbody>
                {(data.items as AuditEntry[]).map((e) => (
                  <tr key={e.id} className="border-b align-top">
                    <td className="whitespace-nowrap py-2 pr-3 tabular-nums">{fmtDateTime(e.created_at)}</td>
                    <td className="py-2 pr-3">{e.user?.name ?? "HousIA"}</td>
                    <td className="py-2">
                      <span className="font-mono text-xs">{e.action}</span>
                      <span className="block text-xs text-muted-foreground">
                        #{e.id} · {e.entity} {e.entity_id}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && data.items.length > 0 && kind === "gestao" && (
          <ul className="space-y-2 text-sm">
            {(data.items as GestaoLine[]).map((l) => (
              <li key={l.id} className="rounded-md border p-2">
                <p>{l.descricao}</p>
                <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span>{l.setor}</span>
                  {l.fhir_refs.map((r) => (
                    <code key={r} className="rounded bg-muted px-1">{r}</code>
                  ))}
                  {l.paciente && (
                    <Link className="inline-flex min-h-9 items-center font-bold text-primary hover:underline" href={`/pacientes/${l.paciente}`}>
                      Abrir paciente
                    </Link>
                  )}
                </p>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
