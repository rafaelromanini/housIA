"use client";

import { CircleCheck, ClipboardCheck, PackageCheck, TriangleAlert } from "lucide-react";

import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { OpmeLine, SurgicalReport } from "@/lib/api";
import { cn } from "@/lib/utils";

const OPME_STATUS: Record<OpmeLine["status"], { label: string; variant: "ok" | "critico" | "atencao" }> = {
  conferido: { label: "Conferido", variant: "ok" },
  nao_autorizado: { label: "Sem autorização", variant: "critico" },
  nao_citado: { label: "Autorizado, não citado", variant: "atencao" },
  quantidade_divergente: { label: "Quantidade divergente", variant: "atencao" },
};

/** Centro cirúrgico: o relatório se monta enquanto o cirurgião dita; pendências anti-glosa e OPME na hora. */
export function SurgicalPanel({ report, patientId }: { report: SurgicalReport; patientId: string }) {
  const filled = report.campos.filter((f) => f.texto).length;
  return (
    <section aria-label="Relatório cirúrgico" className="space-y-3">
      <Card className={cn(report.pendencias.some((p) => p.severidade === "critico") && "border-critico-border")}>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <TriangleAlert aria-hidden className="size-5 text-primary" /> Verificações automáticas
          </CardTitle>
          <CardDescription>Campos obrigatórios, materiais e alergias — o que costuma gerar glosa ou devolução.</CardDescription>
        </CardHeader>
        <CardContent>
          {report.pendencias.length === 0 ? (
            <p className="inline-flex items-center gap-1 text-sm font-bold text-ok">
              <CircleCheck aria-hidden className="size-4" /> Sem pendências.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {report.pendencias.map((p) => (
                <li key={p.texto} className="flex gap-2 text-sm">
                  <SeverityIcon severity={p.severidade} className="mt-0.5 size-4 shrink-0" />
                  <span className="flex-1">
                    {p.texto} <SourceRefs patientId={patientId} refs={p.refs} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <ClipboardCheck aria-hidden className="size-5 text-primary" /> Relatório estruturado
            <span className="text-sm font-normal text-muted-foreground">
              ({filled}/{report.campos.length} campos)
            </span>
          </CardTitle>
          <CardDescription>Cada campo vem do ditado e mostra a fala de origem.</CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="divide-y">
            {report.campos.map((f) => (
              <div key={f.campo} className="grid gap-1 py-2 text-sm sm:grid-cols-[12rem_1fr]">
                <dt className="font-bold">
                  {f.rotulo}
                  {f.obrigatorio && <span className="text-critico"> *</span>}
                </dt>
                <dd>
                  {f.texto ? (
                    <>
                      {f.texto} <SourceRefs patientId={patientId} refs={f.refs} />
                    </>
                  ) : (
                    <span className={cn("italic", f.obrigatorio ? "text-atencao" : "text-muted-foreground")}>
                      {f.obrigatorio ? "Não ditado — obrigatório" : "Não ditado"}
                    </span>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {report.cids.length > 0 && (
            <p className="mt-2 flex flex-wrap items-center gap-1.5 text-sm">
              <span className="font-bold">CID-10:</span>
              {report.cids.map((c) => (
                <Badge key={c.codigo} variant="outline" className="font-mono">
                  {c.codigo} {c.descricao}
                </Badge>
              ))}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <PackageCheck aria-hidden className="size-5 text-primary" /> Conciliação de OPME e materiais
          </CardTitle>
          <CardDescription>Citado no ditado × autorizado na guia. Catálogo e códigos de demonstração (não são TUSS).</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Materiais citados e autorizados</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-1 pr-2">Material</th>
                <th scope="col" className="py-1 pr-2 text-right">Autorizado</th>
                <th scope="col" className="py-1 pr-2 text-right">Citado</th>
                <th scope="col" className="py-1">Situação</th>
              </tr>
            </thead>
            <tbody>
              {report.opme.map((o) => (
                <tr key={o.codigo} className="border-b">
                  <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                    <span className="font-bold">{o.descricao}</span>
                    <span className="block font-mono text-xs text-muted-foreground">{o.codigo}</span>
                  </th>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{o.autorizado}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">{o.citado}</td>
                  <td className="py-1.5">
                    <Badge variant={OPME_STATUS[o.status].variant} className="text-[11px]">
                      {OPME_STATUS[o.status].label}
                    </Badge>
                    <SourceRefs patientId={patientId} refs={o.refs} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </section>
  );
}
