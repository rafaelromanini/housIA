"use client";

import { useCallback, useEffect, useState } from "react";
import { CircleCheck, CircleHelp, CircleMinus, FileDown, Loader2, ShieldCheck } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, downloadFile } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Item = { id: string; texto: string; status: "atendido" | "faltando" | "nao_aplicavel"; evidencia: string; refs: string[]; pergunta: string };
type Doc = { rotulo: string; status: "gerado" | "disponivel" | "faltando"; origem: string; refs: string[] };
type Autorizacao = {
  id: string;
  procedimento: string;
  gatilho_texto: string;
  situacao: "completa" | "incompleta" | "regra_vencida";
  criterios: Item[];
  documentos: Doc[];
  operadora: { informada: string; nome: string; status: string; prazo_dias: number | null; aviso: string };
  regra: { versao: string; fonte: string; vigencia: string; origem: string; conferencia: string };
  faltam: number;
  avisos: string[];
};
type Frase = { texto: string; refs: string[]; origem?: string };
type Pedido = {
  id: number;
  regra_id: string;
  procedimento: string;
  status: "rascunho" | "enviado" | "aprovado" | "negado";
  pacote: { frases: Frase[]; pendencias: { texto: string }[]; anexos: { rotulo: string }[]; avisos: string[]; modo: string };
  revisado: boolean;
  revisado_por: string | null;
  problemas: { frase: number; problema: string }[];
};
type Resposta = { operadora: string; operadoras: string[]; autorizacoes: Autorizacao[]; pedidos: Pedido[]; base: { aviso: string } };

const ITEM = {
  atendido: { label: "Atendido", variant: "ok", Icon: CircleCheck },
  faltando: { label: "Faltando", variant: "atencao", Icon: CircleHelp },
  nao_aplicavel: { label: "Não se aplica", variant: "secondary", Icon: CircleMinus },
} as const;
const STATUS = { rascunho: "Rascunho", enviado: "Enviado", aprovado: "Aprovado", negado: "Negado" } as const;

function reason(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return String((detail as { mensagem: unknown }).mensagem);
  }
  return e instanceof Error ? e.message : fallback;
}

/** Autorização prévia (#56): exigências do Rol/DUT e da operadora, o que falta, e o pacote de justificativa revisado pelo médico. */
export function AuthorizationPanel({ consultId, patientId }: { consultId: number; patientId: string }) {
  const { user } = useAuth();
  const base = `/consultations/${consultId}`;
  const [data, setData] = useState<Resposta | null>(null);
  const [operadora, setOperadora] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await api<Resposta>(`${base}/autorizacoes`);
      setData(out);
      setOperadora(out.operadora);
    } catch {
      setData(null);
    }
  }, [base]);
  useEffect(() => {
    void load();
  }, [load]);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(reason(e, "Não foi possível concluir."));
    } finally {
      setBusy(null);
    }
  }

  if (!data || (data.autorizacoes.length === 0 && data.pedidos.length === 0)) return null;
  const pedidoDe = (id: string) => data.pedidos.find((p) => p.regra_id === id);

  return (
    <section aria-labelledby="aut-prev" className="rounded-lg border bg-card p-4 text-sm" data-testid="autorizacao-previa">
      <h3 id="aut-prev" className="flex items-center gap-2 font-bold">
        <ShieldCheck aria-hidden className="size-4 text-primary" /> Autorização prévia
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Exigências do Rol da ANS (DUT) e da operadora para o que foi pedido nesta consulta. Sem evidência na conversa ou no prontuário, o
        critério fica como faltando. O pacote só sai depois da revisão e aprovação do médico.
      </p>
      {data.base.aviso && (
        <Alert variant="atencao" className="mt-2">
          <span>{data.base.aviso}</span>
        </Alert>
      )}
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1">
          <Label htmlFor="aut-operadora">Operadora do paciente</Label>
          <Input
            id="aut-operadora"
            value={operadora}
            maxLength={60}
            list="aut-operadoras"
            onChange={(e) => setOperadora(e.target.value)}
            placeholder="Nome da operadora"
          />
          <datalist id="aut-operadoras">
            {data.operadoras.map((o) => (
              <option key={o} value={o} />
            ))}
          </datalist>
        </div>
        <Button
          variant="outline"
          disabled={busy !== null}
          onClick={() => run("op", () => api(`${base}/autorizacao/operadora`, { method: "PUT", json: { operadora } }))}
        >
          {busy === "op" && <Loader2 aria-hidden className="animate-spin" />} Salvar operadora
        </Button>
      </div>
      {error && (
        <Alert variant="critico" className="mt-2" data-testid="autorizacao-erro">
          <span>{error}</span>
        </Alert>
      )}
      <ul className="mt-3 space-y-3">
        {data.autorizacoes.map((a) => {
          const pedido = pedidoDe(a.id);
          return (
            <li key={a.id} className="rounded-md border bg-background p-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <h4 className="font-bold">{a.procedimento}</h4>
                <Badge variant={a.situacao === "completa" ? "ok" : a.situacao === "regra_vencida" ? "critico" : "atencao"} className="text-[11px]">
                  {a.situacao === "completa" ? "Critérios atendidos" : a.situacao === "regra_vencida" ? "Regra fora de vigência" : `Faltam ${a.faltam}`}
                </Badge>
                <span className="text-xs text-muted-foreground">{a.gatilho_texto}</span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Regra {a.id} v{a.regra.versao} ({a.regra.vigencia}) · {a.regra.origem === "exemplo" ? "EXEMPLO, não oficial" : `${a.regra.origem}, conferência ${a.regra.conferencia}`}
                {a.operadora.prazo_dias != null && ` · prazo da operadora: ${a.operadora.prazo_dias} dias`}
              </p>
              {a.avisos.map((v) => (
                <p key={v} className="mt-1 text-xs text-atencao">
                  {v}
                </p>
              ))}
              <ul className="mt-2 space-y-1.5">
                {a.criterios.map((i) => {
                  const k = ITEM[i.status];
                  return (
                    <li key={i.id} className="flex flex-wrap items-start gap-1.5">
                      <Badge variant={k.variant} className="text-[11px]">
                        <k.Icon aria-hidden /> {k.label}
                      </Badge>
                      <span className="min-w-0 flex-1">
                        {i.texto}
                        {i.status === "faltando" && i.pergunta && <span className="block text-xs text-atencao">Perguntar ou documentar: {i.pergunta}</span>}
                        {i.evidencia && <span className="block text-xs text-muted-foreground">{i.evidencia}</span>}
                      </span>
                      <SourceRefs patientId={patientId} refs={i.refs} />
                    </li>
                  );
                })}
              </ul>
              {a.documentos.length > 0 && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Documentos: {a.documentos.map((d) => `${d.rotulo} (${d.status === "gerado" ? "gerado no pacote" : d.status === "disponivel" ? "no prontuário" : "falta anexar"})`).join("; ")}
                </p>
              )}
              {a.situacao !== "regra_vencida" && (!pedido || pedido.status === "rascunho") && (
                <Button
                  className="mt-2"
                  variant="outline"
                  disabled={busy !== null}
                  onClick={() => run(`g-${a.id}`, () => api(`${base}/autorizacoes`, { method: "POST", json: { regra_id: a.id } }))}
                >
                  {busy === `g-${a.id}` && <Loader2 aria-hidden className="animate-spin" />} {pedido ? "Refazer pacote de justificativa" : "Gerar pacote de justificativa"}
                </Button>
              )}
              {pedido && <PedidoCard pedido={pedido} base={base} patientId={patientId} medico={user?.role === "medico"} busy={busy} run={run} />}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function PedidoCard({
  pedido,
  base,
  patientId,
  medico,
  busy,
  run,
}: {
  pedido: Pedido;
  base: string;
  patientId: string;
  medico: boolean;
  busy: string | null;
  run: (key: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const url = `${base}/autorizacoes/${pedido.id}`;
  const [erro, setErro] = useState<string | null>(null);
  async function baixar(ext: "pdf" | "json") {
    setErro(null);
    try {
      await downloadFile(`${url}/export.${ext}`, `autorizacao-${pedido.id}.${ext}`);
    } catch (e) {
      setErro(reason(e, "Não foi possível exportar."));
    }
  }
  const mudar = (status: string, protocolo = "") => run(`s-${pedido.id}-${status}`, () => api(`${url}/status`, { method: "PUT", json: { status, protocolo } }));
  return (
    <div className="mt-3 rounded-md border bg-card p-3" data-testid="autorizacao-pedido">
      <div className="flex flex-wrap items-center gap-1.5">
        <b>Pacote de justificativa</b>
        <Badge variant={pedido.status === "aprovado" ? "ok" : pedido.status === "negado" ? "critico" : "outline"} className="text-[11px]">
          {STATUS[pedido.status]}
        </Badge>
        <Badge variant={pedido.revisado ? "ok" : "atencao"} className="text-[11px]">
          {pedido.revisado ? `Revisado por ${pedido.revisado_por ?? "médico"}` : "Aguardando revisão do médico"}
        </Badge>
        {pedido.pacote.modo === "ia" && <Badge variant="ai">Reescrito por IA, com as mesmas fontes</Badge>}
      </div>
      <ol className="mt-2 list-decimal space-y-1 pl-5">
        {pedido.pacote.frases.map((f, n) => (
          <li key={n}>
            {f.texto} <SourceRefs patientId={patientId} refs={f.refs} />
          </li>
        ))}
      </ol>
      {pedido.pacote.pendencias.length > 0 && (
        <p className="mt-2 text-xs text-atencao">Pendências (não afirmadas na justificativa): {pedido.pacote.pendencias.map((p) => p.texto).join("; ")}</p>
      )}
      {pedido.pacote.anexos.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Anexos: {pedido.pacote.anexos.map((x) => x.rotulo).join("; ")}</p>}
      {pedido.pacote.avisos.map((v) => (
        <p key={v} className="mt-1 text-xs text-atencao">
          {v}
        </p>
      ))}
      {pedido.problemas.length > 0 && <p className="mt-1 text-xs text-critico">Afirmação sem evidência válida: corrija antes de aprovar.</p>}
      {erro && <p className="mt-1 text-xs text-critico">{erro}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {pedido.status === "rascunho" && !pedido.revisado && (
          <Button disabled={!medico || busy !== null} title={medico ? undefined : "Só o médico aprova"} onClick={() => run(`a-${pedido.id}`, () => api(`${url}/aprovar`, { method: "POST" }))}>
            {busy === `a-${pedido.id}` && <Loader2 aria-hidden className="animate-spin" />} Revisar e aprovar pacote
          </Button>
        )}
        {pedido.revisado && (
          <>
            <Button variant="outline" onClick={() => baixar("pdf")}>
              <FileDown aria-hidden /> Baixar PDF
            </Button>
            <Button variant="outline" onClick={() => baixar("json")}>
              <FileDown aria-hidden /> Baixar JSON (FHIR)
            </Button>
          </>
        )}
        {pedido.status === "rascunho" && pedido.revisado && (
          <Button disabled={busy !== null} onClick={() => mudar("enviado")}>
            Marcar como enviado
          </Button>
        )}
        {pedido.status === "enviado" && (
          <>
            <Button variant="outline" disabled={busy !== null} onClick={() => mudar("aprovado")}>
              Operadora aprovou
            </Button>
            <Button variant="outline" disabled={busy !== null} onClick={() => mudar("negado")}>
              Operadora negou
            </Button>
          </>
        )}
        {pedido.status === "negado" && (
          <Button variant="outline" disabled={busy !== null} onClick={() => mudar("rascunho")}>
            Preparar recurso
          </Button>
        )}
      </div>
    </div>
  );
}
