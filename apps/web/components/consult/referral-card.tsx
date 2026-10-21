"use client";

import { useState } from "react";
import { CircleCheck, CircleHelp, CircleX, FileText, Loader2, PhoneCall, Siren, Stethoscope, Undo2 } from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, type ConsultInsights, type Encaminhamento, type EscolhaEncaminhamento } from "@/lib/api";
import { cn } from "@/lib/utils";

const MIN_MOTIVO = 10;
const CRITERIO = {
  atendido: { label: "Atendido", variant: "ok", Icon: CircleCheck },
  nao_atendido: { label: "Não atendido", variant: "secondary", Icon: CircleX },
  confirmar: { label: "A confirmar", variant: "atencao", Icon: CircleHelp },
} as const;
const EXAME = {
  feito: { label: "No prontuário", variant: "ok" },
  desatualizado: { label: "Desatualizado", variant: "atencao" },
  faltando: { label: "Falta", variant: "critico" },
} as const;
const ESCOLHA = {
  pendente: { label: "Para decidir", variant: "outline" },
  encaminhar: { label: "Encaminhar", variant: "info" },
  manejar_aps: { label: "Manejo na APS", variant: "ok" },
} as const;

function apiMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return String((detail as { mensagem: string }).mensagem);
  }
  return e instanceof Error ? e.message : "Erro ao registrar a escolha";
}

/** Antes de encaminhar (#29): critérios atendidos e não atendidos, exames mínimos, manejo na APS e rascunho do pedido. */
export function ReferralCard({
  consultId,
  items,
  patientId,
  editable,
  onInsights,
}: {
  consultId: number;
  items: Encaminhamento[];
  patientId: string;
  /** só com a consulta em andamento o profissional escolhe; sem isso o card é leitura */
  editable: boolean;
  onInsights: (insights: ConsultInsights) => void;
}) {
  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Nenhum encaminhamento em discussão. O card abre quando o profissional diz que vai encaminhar, ou quando uma hipótese já cumpre
        critério de encaminhamento.
      </p>
    );
  }
  return (
    <ul className="space-y-3">
      {items.map((e) => (
        <ReferralItem key={e.id} enc={e} consultId={consultId} patientId={patientId} editable={editable} onInsights={onInsights} />
      ))}
    </ul>
  );
}

function ReferralItem({
  enc,
  consultId,
  patientId,
  editable,
  onInsights,
}: {
  enc: Encaminhamento;
  consultId: number;
  patientId: string;
  editable: boolean;
  onInsights: (insights: ConsultInsights) => void;
}) {
  const { patient } = usePatient();
  const r = (t: string) => reidentify(t, patient.nome);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [motivo, setMotivo] = useState(enc.decisao.motivo);
  const [draft, setDraft] = useState(false);
  const missing = enc.exames_faltantes.length > 0;
  const choice = ESCOLHA[enc.decisao.escolha];
  const needsReason = missing && motivo.trim().length < MIN_MOTIVO;
  const idBase = `enc-${enc.id}`;

  async function decide(escolha: EscolhaEncaminhamento) {
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ insights: ConsultInsights }>(`/consultations/${consultId}/encaminhamentos/${encodeURIComponent(enc.id)}`, {
        method: "PUT",
        json: { escolha, motivo: motivo.trim() },
      });
      onInsights(out.insights);
    } catch (e) {
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className={cn("rounded-md border bg-background p-3 text-sm", missing && enc.decisao.escolha === "pendente" && "border-atencao-border")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Stethoscope aria-hidden className="size-4 text-primary" />
        <h4 className="font-bold">
          Encaminhar para {enc.especialidade}: {enc.condicao}
        </h4>
        <Badge variant={choice.variant} className="text-[11px]">
          {choice.label}
        </Badge>
        <span className="text-xs text-muted-foreground">{r(enc.gatilho_texto)}</span>
      </div>

      <p className={cn("mt-2 rounded-md border px-2 py-1.5", missing ? "border-atencao-border bg-atencao-bg" : "border-ok-border bg-ok-bg")}>
        {enc.recomendacao}
      </p>

      <section aria-labelledby={`${idBase}-crit`} className="mt-3">
        <h5 id={`${idBase}-crit`} className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
          Critérios de encaminhamento
        </h5>
        <ul className="mt-1 space-y-1.5">
          {enc.criterios.map((c) => {
            const k = CRITERIO[c.status];
            return (
              <li key={c.texto} className="flex flex-wrap items-start gap-1.5">
                <Badge variant={k.variant} className="text-[11px]">
                  <k.Icon aria-hidden /> {k.label}
                </Badge>
                <span className="min-w-0 flex-1">
                  {c.texto}
                  {c.evidencia && <span className="block text-xs text-muted-foreground">{r(c.evidencia)}</span>}
                </span>
                <SourceRefs patientId={patientId} refs={c.refs} />
              </li>
            );
          })}
        </ul>
      </section>

      <section aria-labelledby={`${idBase}-ex`} className="mt-3">
        <h5 id={`${idBase}-ex`} className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
          Exames mínimos antes de encaminhar
        </h5>
        <ul className="mt-1 space-y-1.5">
          {enc.exames.map((x) => {
            const k = EXAME[x.status];
            return (
              <li key={x.exame} className="flex flex-wrap items-center gap-1.5">
                <Badge variant={k.variant} className="text-[11px]">
                  {k.label}
                </Badge>
                <span className="font-medium">{x.exame}</span>
                {x.ultimo && <span className="text-xs text-muted-foreground">{x.ultimo}</span>}
                <SourceRefs patientId={patientId} refs={x.refs} />
              </li>
            );
          })}
        </ul>
      </section>

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <div className="rounded-md bg-muted/50 p-2">
          <p className="text-xs font-bold">Manejo possível na APS</p>
          <p className="mt-0.5">{enc.manejo_aps}</p>
        </div>
        <div className="rounded-md border border-critico-border bg-critico-bg p-2">
          <p className="flex items-center gap-1 text-xs font-bold">
            <Siren aria-hidden className="size-3.5" /> Sinais de urgência
          </p>
          <p className="mt-0.5">{enc.sinais_urgencia}</p>
        </div>
      </div>
      <p className="mt-2 flex items-start gap-1.5">
        <PhoneCall aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" /> {enc.teleconsultoria}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">Fonte: {enc.fonte}</p>

      <div className="mt-2">
        <Button type="button" variant="outline" size="sm" aria-expanded={draft} onClick={() => setDraft((v) => !v)}>
          <FileText aria-hidden /> {draft ? "Ocultar" : "Ver"} rascunho do encaminhamento
        </Button>
        {draft && (
          <div className="mt-2 rounded-md border bg-muted/30 p-2 text-xs">
            <p className="font-bold">ServiceRequest em rascunho (nada vai ao prontuário sem o envio revisado)</p>
            <pre className="mt-1 whitespace-pre-wrap font-sans">
              {r(((enc.rascunho.note as { text: string }[] | undefined)?.[0]?.text) ?? "")}
            </pre>
          </div>
        )}
      </div>

      {editable && (
        <div className="mt-3 space-y-2 border-t pt-2">
          <div>
            <Label htmlFor={`${idBase}-motivo`} className="text-xs">
              {missing ? `Justificativa (obrigatória para encaminhar sem ${enc.exames_faltantes.join(", ")})` : "Observação (opcional)"}
            </Label>
            <Textarea
              id={`${idBase}-motivo`}
              rows={2}
              maxLength={500}
              value={motivo}
              onChange={(ev) => setMotivo(ev.target.value)}
              className="mt-1"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" size="sm" disabled={busy || needsReason} onClick={() => decide("encaminhar")}>
              {busy && <Loader2 aria-hidden className="animate-spin" />} Encaminhar
            </Button>
            <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={() => decide("manejar_aps")}>
              Manejar na APS
            </Button>
            {enc.decisao.escolha !== "pendente" && (
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => decide("pendente")}>
                <Undo2 aria-hidden /> Desfazer
              </Button>
            )}
            {enc.decisao.por && enc.decisao.escolha !== "pendente" && (
              <span className="text-xs text-muted-foreground">Escolha de {enc.decisao.por}</span>
            )}
          </div>
          {error && (
            <p role="alert" className="text-sm text-critico">
              {error}
            </p>
          )}
        </div>
      )}
    </li>
  );
}
