"use client";

import { Download, RefreshCw, ShieldOff } from "lucide-react";
import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { downloadFile, type Dossie } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useApi } from "@/lib/use-api";

const STATUS_LABEL = { implementado: "Implementado", parcial: "Parcial", nao_implementado: "Não implementado" } as const;

export function ConformidadeView() {
  const { user } = useAuth();
  const allowed = user?.role === "gestor";
  const d = useApi<Dossie>(allowed ? "/compliance/dossie" : null);
  const [error, setError] = useState<string | null>(null);

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden />O dossiê de conformidade é restrito ao perfil de gestão.
      </Alert>
    );
  }
  const baixar = async () => {
    setError(null);
    try {
      await downloadFile("/compliance/dossie?formato=md", `dossie-conformidade-${d.data?.versao ?? "atual"}.md`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível baixar.");
    }
  };
  const x = d.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Conformidade</h1>
          <p className="text-sm text-muted-foreground">
            RoPA, RIPD resumido e matriz CFM/LGPD. {x ? `Versão ${x.versao}, de ${x.atualizado_em}.` : ""}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void d.reload()}>
            <RefreshCw aria-hidden /> Atualizar
          </Button>
          <Button variant="outline" onClick={() => void baixar()}>
            <Download aria-hidden /> Baixar em Markdown
          </Button>
        </div>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {d.error && <Alert variant="critico">{d.error}</Alert>}
      {!x && !d.error && <Skeleton className="h-64" />}
      {x && (
        <>
          <Alert variant="atencao">
            {x.aviso} Itens que pedem revisão jurídica humana: {x.precisa_revisao_juridica.length}.
          </Alert>
          <Card>
            <CardHeader>
              <CardTitle>Estado do sistema agora</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 text-sm">
              <p>
                Cadeia da auditoria:{" "}
                <strong>
                  {x.calculado.cadeia_audit_log.integra
                    ? `íntegra (${x.calculado.cadeia_audit_log.encadeadas} encadeadas)`
                    : `QUEBRADA no registro ${x.calculado.cadeia_audit_log.primeira_quebra?.id}`}
                </strong>
              </p>
              <p>
                Acessos por quebra de vidro: {x.calculado.acessos_quebra_de_vidro.total} ({x.calculado.acessos_quebra_de_vidro.ultimos_90_dias} nos últimos 90 dias)
              </p>
              <p>
                Retenção da transcrição:{" "}
                {x.calculado.retencao.configurada
                  ? Object.entries(x.calculado.retencao.transcricao_dias_por_tipo)
                      .map(([k, v]) => `${k}: ${v ?? "sem expurgo"}`)
                      .join(" · ")
                  : "não configurada (sem expurgo automático)"}
              </p>
              <p>Criptografia de campo: {x.calculado.criptografia_de_campo ? "ligada" : "desligada"}</p>
              <p>Provedor de IA: {x.calculado.provedor_de_llm.provedor ?? `nenhum (modo ${x.calculado.provedor_de_llm.modo})`}</p>
              <p>Suboperadores ativos: {x.calculado.suboperadores_ativos.join(", ") || "nenhum"}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>RoPA — operações de tratamento</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {x.ropa.map((o) => (
                <details key={o.id} className="rounded-lg border p-3">
                  <summary className="cursor-pointer font-bold">
                    {o.id} — {o.operacao} {o.revisao_juridica && <Badge variant="atencao">revisão jurídica</Badge>}
                  </summary>
                  <dl className="mt-2 space-y-1 text-sm">
                    <div><dt className="inline font-bold">Dados: </dt><dd className="inline">{o.dados}</dd></div>
                    <div><dt className="inline font-bold">Finalidade: </dt><dd className="inline">{o.finalidade}</dd></div>
                    <div><dt className="inline font-bold">Base legal (proposta): </dt><dd className="inline">{o.base_legal}</dd></div>
                    <div><dt className="inline font-bold">Retenção: </dt><dd className="inline">{o.retencao}</dd></div>
                    <div><dt className="inline font-bold">Operadores: </dt><dd className="inline">{o.operadores.join(", ")}</dd></div>
                  </dl>
                </details>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>RIPD resumido</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <p>{x.ripd.fluxo}</p>
              <ul className="list-disc space-y-1 pl-5">
                {x.ripd.riscos.map((r) => (
                  <li key={r.risco}>
                    <strong>{r.risco}.</strong> Medida: {r.medida} Residual: {r.residual}
                  </li>
                ))}
              </ul>
              <p className="font-bold">Pendências</p>
              <ul className="list-disc pl-5">
                {x.ripd.pendencias.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Matriz CFM e LGPD</CardTitle>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <caption className="sr-only">Artigo, controle implementado, onde no código e no teste</caption>
                <thead>
                  <tr className="border-b text-left">
                    <th scope="col" className="py-2 pr-3">Norma e artigo</th>
                    <th scope="col" className="py-2 pr-3">Controle</th>
                    <th scope="col" className="py-2 pr-3">Status</th>
                    <th scope="col" className="py-2">Código e testes</th>
                  </tr>
                </thead>
                <tbody>
                  {x.matriz.map((m) => (
                    <tr key={`${m.norma}-${m.artigo}-${m.exigencia}`} className="border-b align-top">
                      <td className="py-2 pr-3">
                        {m.norma} {m.artigo}
                        <span className="block text-xs text-muted-foreground">{m.exigencia}</span>
                      </td>
                      <td className="py-2 pr-3">{m.controle}</td>
                      <td className="py-2 pr-3">
                        <Badge variant={m.status === "implementado" ? "ok" : m.status === "parcial" ? "atencao" : "critico"}>{STATUS_LABEL[m.status]}</Badge>
                        {m.revisao_juridica && <span className="block text-xs text-muted-foreground">revisão jurídica</span>}
                      </td>
                      <td className="py-2 font-mono text-[11px]">{[...m.codigo, ...m.testes].join("\n") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
