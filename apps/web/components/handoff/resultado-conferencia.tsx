"use client";

import { CircleCheck, TriangleAlert, XCircle } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityBadge } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { type HandoffCheck, type HandoffCheckItem } from "@/lib/api";
import { cn } from "@/lib/utils";

const GROUPS: { status: HandoffCheckItem["status"]; title: string; Icon: typeof XCircle; tone: string }[] = [
  { status: "divergente", title: "Divergências com o prontuário", Icon: TriangleAlert, tone: "text-critico" },
  { status: "omitido", title: "Não mencionado no relato", Icon: XCircle, tone: "text-atencao" },
  { status: "mencionado", title: "Mencionado", Icon: CircleCheck, tone: "text-ok" },
];

/** Resultado da conferência: divergências, omissões e o que foi mencionado, cada um com a fonte (e a fala, ao vivo).
 * `parcial`: a passagem ainda está em andamento, então "omitido" quer dizer "ainda não citado". */
export function ResultadoConferencia({ result, parcial = false }: { result: HandoffCheck; parcial?: boolean }) {
  const { patient } = usePatient();
  return (
    <div className="space-y-3" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <AiBadge />
        {result.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
        {result.meta.modo === "regras" && <Badge variant="outline">Regras do prontuário (tempo real)</Badge>}
        <Badge variant={result.contagem.divergente ? "critico" : result.contagem.omitido ? "atencao" : "ok"}>
          Cobertura {result.cobertura != null ? `${Math.round(result.cobertura * 100)}%` : "—"}
        </Badge>
      </div>
      <p className="font-bold">{reidentify(result.resumo, patient.nome)}</p>
      {GROUPS.map(({ status, title, Icon, tone }) => {
        const items = result.itens.filter((i) => i.status === status);
        if (items.length === 0) return null;
        const heading = parcial && status === "omitido" ? "Ainda não citado" : title;
        return (
          <section key={status} aria-label={heading} data-testid={`grupo-${status}`}>
            <h3 className={cn("mb-1.5 flex items-center gap-1.5 text-sm font-bold", tone)}>
              <Icon aria-hidden className="size-4" /> {heading} ({items.length})
            </h3>
            <ul className="space-y-1.5">
              {items.map((it, i) => (
                <li
                  key={i}
                  data-testid="item-conferencia"
                  className={cn(
                    "rounded-md border p-2.5 text-sm",
                    status === "divergente" && "border-critico-border bg-critico-bg/60",
                    status === "omitido" && "border-atencao-border bg-atencao-bg/60",
                    status === "omitido" && it.severidade === "critico" && "border-critico-border bg-critico-bg/60",
                  )}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityBadge severity={it.severidade} />
                    <span className="font-bold">{reidentify(it.fato, patient.nome)}</span>
                    {it.fala_ref && <Badge variant="outline">{it.fala_ref.replace("/", " ")}</Badge>}
                  </div>
                  <p className="mt-1">
                    {parcial && status === "omitido"
                      ? "Ainda não aparece no que foi dito."
                      : reidentify(it.comentario, patient.nome)}
                  </p>
                  <SourceRefs patientId={patient.id} refs={it.refs} />
                </li>
              ))}
            </ul>
          </section>
        );
      })}
      {!parcial && <FeedbackButtons kind="handoff_check" patientId={patient.id} />}
    </div>
  );
}
