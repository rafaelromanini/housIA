"use client";

import { useState } from "react";
import { CircleCheck, FlaskConical, Loader2, Pencil, Pill, Printer, Undo2, XCircle } from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { SeverityIcon } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, type ConsultInsights, type OrderDecisionIn, type OrderDraft } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const MIN_JUSTIFICATION = 15;
const ACTION_LABEL: Record<OrderDraft["acao"], string> = {
  iniciar: "Iniciar",
  alterar_dose: "Alterar dose",
  suspender: "Suspender",
  solicitar: "Exame",
};
const SOURCE_LABEL = { fala: "", protocolo: "Sugerido pelo protocolo", hipotese: "Sugerido pela hipótese" } as const;
const STATE = {
  pendente: { label: "Para decidir", variant: "outline" },
  assinado: { label: "Assinado", variant: "ok" },
  recusado: { label: "Recusado", variant: "secondary" },
} as const;

/** Mensagem do 409/422 da API (o `detail` pode ser um objeto com `mensagem`). */
function apiMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return String((detail as { mensagem: string }).mensagem);
  }
  return e instanceof Error ? e.message : "Erro ao registrar a decisão";
}

export function posologia(o: OrderDraft): string {
  return [o.dose, o.via, o.frequencia, o.duracao].filter(Boolean).join(" · ");
}

/** Pedidos em rascunho: cada item mostra a fala de origem, as checagens de segurança e a assinatura item a item. */
export function OrdersCard({
  consultId,
  orders,
  editable,
  onInsights,
}: {
  consultId: number;
  orders: OrderDraft[];
  editable: boolean;
  onInsights: (insights: ConsultInsights) => void;
}) {
  const { patient } = usePatient();
  const r = (t: string) => reidentify(t, patient.nome);
  const signedRx = orders.filter((o) => o.tipo === "medicamento" && o.decisao.estado === "assinado");
  const signedExams = orders.filter((o) => o.tipo === "exame" && o.decisao.estado === "assinado");
  const pending = orders.filter((o) => o.decisao.estado === "pendente").length;

  function print(kind: "receita" | "exames") {
    const cls = `printing-pedido-${kind}`;
    document.body.classList.add(cls);
    const done = () => {
      document.body.classList.remove(cls);
      window.removeEventListener("afterprint", done);
    };
    window.addEventListener("afterprint", done);
    window.print();
  }

  if (orders.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Nenhum pedido ainda. Quando o profissional disser “vou pedir hemograma” ou “vou começar amoxicilina 500 mg de 8 em 8 horas”,
        o rascunho aparece aqui, já checado contra alergias, função renal e o que o paciente usa.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {pending > 0 ? `${pending} para decidir` : "Todos decididos"} · {signedRx.length + signedExams.length} assinado
        {signedRx.length + signedExams.length === 1 ? "" : "s"}. Só o que for assinado vai ao prontuário, como rascunho (status “draft”).
      </p>
      <ul className="space-y-2">
        {orders.map((o) => (
          <OrderItem key={o.id} consultId={consultId} order={o} editable={editable} onInsights={onInsights} r={r} patientId={patient.id} />
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => print("receita")} disabled={signedRx.length === 0}>
          <Printer aria-hidden /> Imprimir receita ({signedRx.length})
        </Button>
        <Button variant="outline" size="sm" onClick={() => print("exames")} disabled={signedExams.length === 0}>
          <Printer aria-hidden /> Imprimir solicitação de exames ({signedExams.length})
        </Button>
      </div>
      <PrintDocuments rx={signedRx} exams={signedExams} />
    </div>
  );
}

function OrderItem({
  consultId,
  order: o,
  editable,
  onInsights,
  r,
  patientId,
}: {
  consultId: number;
  order: OrderDraft;
  editable: boolean;
  onInsights: (insights: ConsultInsights) => void;
  r: (t: string) => string;
  patientId: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justification, setJustification] = useState(o.decisao.justificativa);
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState({ dose: o.dose, via: o.via, frequencia: o.frequencia, duracao: o.duracao });
  const critical = o.gravidade === "critico";
  const state = STATE[o.decisao.estado];
  const needsJustification = critical && justification.trim().length < MIN_JUSTIFICATION;
  const missingDose = o.tipo === "medicamento" && o.acao !== "suspender" && !(editing ? edit.dose : o.dose).trim();

  async function decide(estado: OrderDecisionIn["estado"]) {
    setBusy(true);
    setError(null);
    const body: OrderDecisionIn = { estado, justificativa: justification.trim() };
    if (editing) Object.assign(body, edit);
    try {
      const out = await api<{ insights: ConsultInsights }>(`/consultations/${consultId}/pedidos/${encodeURIComponent(o.id)}`, {
        method: "PUT",
        json: body,
      });
      onInsights(out.insights);
      setEditing(false);
    } catch (e) {
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const Icon = o.tipo === "medicamento" ? Pill : FlaskConical;
  const source = SOURCE_LABEL[o.fonte_pedido];
  return (
    <li
      className={cn(
        "rounded-md border bg-background p-2.5 text-sm",
        critical && o.decisao.estado === "pendente" && "border-critico-border",
        o.decisao.estado === "recusado" && "opacity-70",
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Icon aria-hidden className="size-4 text-primary" />
        <p className="font-bold">{r(o.item)}</p>
        <Badge variant={o.acao === "suspender" ? "atencao" : "info"} className="text-[11px]">
          {ACTION_LABEL[o.acao]}
        </Badge>
        <Badge variant={state.variant} className="text-[11px]">
          {o.decisao.estado === "assinado" && <CircleCheck aria-hidden />}
          {o.decisao.estado === "recusado" && <XCircle aria-hidden />}
          {state.label}
        </Badge>
        {critical && o.decisao.estado === "pendente" && (
          <Badge variant="critico" className="text-[11px]">
            Alerta crítico
          </Badge>
        )}
        {source && (
          <Badge variant="ai" className="text-[11px]">
            {source}
          </Badge>
        )}
        {o.origem === "ia" && (
          <Badge variant="ai" className="text-[11px]">
            Posologia completada pela IA
          </Badge>
        )}
        <SourceRefs patientId={patientId} refs={o.refs} label="fonte" />
      </div>

      {o.acao === "alterar_dose" && o.dose_anterior && (
        <p className="mt-1">
          Dose atual <span className="font-bold">{o.dose_anterior}</span> → <span className="font-bold">{o.dose || "dose a definir"}</span>
          {[o.via, o.frequencia, o.duracao].some(Boolean) && ` · ${[o.via, o.frequencia, o.duracao].filter(Boolean).join(" · ")}`}
        </p>
      )}
      {o.acao !== "alterar_dose" && posologia(o) && <p className="mt-1">{posologia(o)}</p>}
      {o.tipo === "medicamento" && o.acao !== "suspender" && !o.dose && !editing && (
        <p className="mt-1 text-atencao">Dose não dita: informe antes de assinar.</p>
      )}
      {o.justificativa && <p className="mt-0.5 text-muted-foreground">{r(o.justificativa)}</p>}
      {o.tipo === "exame" && o.preparo && <p className="mt-0.5 text-xs text-muted-foreground">Preparo: {o.preparo}</p>}
      {o.tipo === "exame" && o.loinc && (
        <p className="text-xs text-muted-foreground">
          LOINC <span className="font-mono">{o.loinc}</span>
        </p>
      )}

      {o.checagens.length > 0 && (
        <ul className="mt-1.5 space-y-1" aria-label={`Checagens de segurança de ${o.item}`}>
          {o.checagens.map((c) => (
            <li key={c.tipo + c.texto} className="flex gap-1.5">
              <SeverityIcon severity={c.severidade} className="mt-0.5 size-4 shrink-0" />
              <span className="flex-1">
                {r(c.texto)} <SourceRefs patientId={patientId} refs={c.refs} />
              </span>
            </li>
          ))}
        </ul>
      )}

      {o.decisao.aviso && (
        <p className="mt-1 text-xs font-medium text-critico" role="alert">
          {o.decisao.aviso}
        </p>
      )}

      {o.decisao.estado !== "pendente" && (
        <p className="mt-1 text-xs text-muted-foreground">
          {o.decisao.estado === "assinado" ? "Assinado" : "Recusado"} por {o.decisao.por}
          {o.decisao.em && ` em ${fmtDateTime(o.decisao.em)}`}
          {o.decisao.editado && " · posologia corrigida pelo profissional"}
          {o.decisao.justificativa && ` · justificativa: ${o.decisao.justificativa}`}
        </p>
      )}

      {editable && o.decisao.estado === "pendente" && (
        <div className="mt-2 space-y-2">
          {editing && o.tipo === "medicamento" && (
            <div className="grid gap-2 sm:grid-cols-4">
              {(
                [
                  ["dose", "Dose"],
                  ["via", "Via"],
                  ["frequencia", "Frequência"],
                  ["duracao", "Duração"],
                ] as const
              ).map(([key, label]) => (
                <div key={key}>
                  <Label htmlFor={`${o.id}-${key}`} className="text-xs">
                    {label}
                  </Label>
                  <Input id={`${o.id}-${key}`} value={edit[key]} onChange={(e) => setEdit((v) => ({ ...v, [key]: e.target.value }))} />
                </div>
              ))}
            </div>
          )}
          {critical && (
            <div>
              <Label htmlFor={`${o.id}-just`} className="text-xs">
                Justificativa para assinar com alerta crítico (mínimo {MIN_JUSTIFICATION} caracteres; fica na auditoria)
              </Label>
              <Textarea id={`${o.id}-just`} value={justification} onChange={(e) => setJustification(e.target.value)} className="min-h-16" />
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm font-bold text-critico">
              {error}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => void decide("assinado")} disabled={busy || needsJustification || missingDose}>
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />} Assinar
            </Button>
            <Button size="sm" variant="outline" onClick={() => void decide("recusado")} disabled={busy}>
              <XCircle aria-hidden /> Recusar
            </Button>
            {o.tipo === "medicamento" && (
              <Button size="sm" variant="outline" onClick={() => setEditing((v) => !v)} disabled={busy} aria-pressed={editing}>
                <Pencil aria-hidden /> {editing ? "Cancelar edição" : "Editar posologia"}
              </Button>
            )}
          </div>
        </div>
      )}
      {editable && o.decisao.estado !== "pendente" && (
        <div className="mt-2">
          {error && (
            <p role="alert" className="mb-1 text-sm font-bold text-critico">
              {error}
            </p>
          )}
          <Button size="sm" variant="outline" onClick={() => void decide("pendente")} disabled={busy}>
            <Undo2 aria-hidden /> Desfazer decisão
          </Button>
        </div>
      )}
    </li>
  );
}

/** Documentos de impressão (receita e solicitação de exames): só os itens assinados; invisíveis na tela. */
function PrintDocuments({ rx, exams }: { rx: OrderDraft[]; exams: OrderDraft[] }) {
  const { patient } = usePatient();
  const { user } = useAuth();
  const today = new Date().toLocaleDateString("pt-BR");
  const footer =
    "Rascunho gerado com apoio de IA (HousIA) e assinado item a item pelo profissional. Não vale como documento oficial até ser emitido e assinado no sistema do serviço.";
  return (
    <>
      <section id="pedido-receita" aria-label="Receita para impressão" className="pedido-doc space-y-3">
        <h2 className="text-xl font-bold">Receita (rascunho)</h2>
        <p>
          Paciente: <strong>{patient.nome}</strong> · {today} · {user?.name}
        </p>
        <ol className="list-decimal space-y-2 pl-6">
          {rx.map((o) => (
            <li key={o.id}>
              <strong>{o.acao === "suspender" ? `Suspender ${o.item}` : o.item}</strong>
              {o.acao === "alterar_dose" && o.dose_anterior && ` (dose atual ${o.dose_anterior})`}
              {posologia(o) && <span> — {posologia(o)}</span>}
              {o.decisao.justificativa && <div className="text-sm">Justificativa: {o.decisao.justificativa}</div>}
            </li>
          ))}
        </ol>
        <p className="text-xs">{footer}</p>
      </section>
      <section id="pedido-exames" aria-label="Solicitação de exames para impressão" className="pedido-doc space-y-3">
        <h2 className="text-xl font-bold">Solicitação de exames (rascunho)</h2>
        <p>
          Paciente: <strong>{patient.nome}</strong> · {today} · {user?.name}
        </p>
        <ol className="list-decimal space-y-2 pl-6">
          {exams.map((o) => (
            <li key={o.id}>
              <strong>{o.item}</strong>
              {o.justificativa && <span> — {o.justificativa}</span>}
              {o.preparo && <div className="text-sm">Preparo: {o.preparo}</div>}
            </li>
          ))}
        </ol>
        <p className="text-xs">{footer}</p>
      </section>
    </>
  );
}
