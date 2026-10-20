"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { CircleCheck, ClipboardList, Clock, RefreshCw, Route } from "lucide-react";

import { ContactDialog, MessageDialog } from "@/components/journey/contact-dialog";
import { MetricsPanel } from "@/components/journey/metrics-panel";
import { SeverityBadge } from "@/components/severity";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, type JourneyItem, type JourneyTask, type JourneyWorklist, type Role } from "@/lib/api";
import { fmtDateTime, ROLE_LABEL } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

const RULES: Record<string, string> = {
  alta_sem_contato_72h: "Alta de alto risco sem contato",
  retorno_nao_agendado: "Retorno não agendado",
  falta_ao_retorno: "Falta à consulta",
  protocolo_aps_vencido: "Protocolo de APS vencido",
  nao_adesao_consulta: "Sinal de não adesão",
  troca_sem_reconciliacao: "Troca sem reconciliação",
};
type Tab = "lista" | "tarefas" | "metricas";
const TABS: { key: Tab; label: string }[] = [
  { key: "lista", label: "Pacientes em risco" },
  { key: "tarefas", label: "Tarefas" },
  { key: "metricas", label: "Métricas" },
];

function Tile({ label, value, tone }: { label: string; value: number; tone?: "critico" }) {
  return (
    <div className={cn("rounded-lg border bg-card p-3 shadow-sm", tone === "critico" && value > 0 && "border-critico-border")}>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-1 text-3xl font-bold leading-none">{value.toLocaleString("pt-BR")}</p>
    </div>
  );
}

/** Waze da jornada: quem saiu do radar entre um atendimento e outro, com a fonte de cada item e o botão de tarefa. */
export function JourneyView() {
  const [tab, setTab] = useState<Tab>("lista");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <Route aria-hidden className="size-6 text-primary" /> Jornada do paciente
        </h1>
        <p className="text-sm text-muted-foreground">
          Regras determinísticas sobre o prontuário (FHIR) e o HousIA: quem teve alta e ninguém procurou, quem faltou, quem está sem retorno.
          Heurísticas de demonstração, dados sintéticos.
        </p>
      </div>
      <div role="tablist" aria-label="Jornada" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1 sm:w-fit">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              "min-h-10 rounded-md px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              tab === t.key ? "bg-card text-primary shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "lista" && <Worklist />}
      {tab === "tarefas" && <Tasks />}
      {tab === "metricas" && <MetricsPanel />}
    </div>
  );
}

// ---------- lista ----------
function Worklist() {
  const [rule, setRule] = useState("");
  const [role, setRole] = useState("");
  const query = useMemo(() => {
    const p = new URLSearchParams();
    if (rule) p.set("regra", rule);
    if (role) p.set("papel", role);
    return p.toString();
  }, [rule, role]);
  const { data, error, loading, reload } = useApi<JourneyWorklist>(`/journey/worklist${query ? `?${query}` : ""}`);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="f-regra">Regra</Label>
          <NativeSelect id="f-regra" value={rule} onChange={(e) => setRule(e.target.value)}>
            <option value="">Todas as regras</option>
            {Object.entries(RULES).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="f-papel">Responsável sugerido</Label>
          <NativeSelect id="f-papel" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">Todos os perfis</option>
            <option value="enfermagem">Enfermagem</option>
            <option value="medico">Médico(a)</option>
            <option value="gestor">Gestão</option>
          </NativeSelect>
        </div>
        <Button variant="outline" onClick={() => void reload()} disabled={loading}>
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-48 w-full" />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Tile label="Itens na lista" value={data.resumo.itens} />
            <Tile label="Pacientes" value={data.resumo.pacientes} />
            <Tile label="Críticos" value={data.resumo.criticos} tone="critico" />
            <Tile label="Com tarefa aberta" value={data.resumo.com_tarefa} />
          </div>
          {data.itens.length === 0 ? (
            <Alert variant="ok">
              <CircleCheck aria-hidden />
              Ninguém na lista com os filtros atuais.
            </Alert>
          ) : (
            <ul className="space-y-2" aria-label="Pacientes em risco de abandono">
              {data.itens.map((item) => (
                <li key={item.id}>
                  <ItemCard item={item} onChange={() => void reload()} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function ItemCard({ item, onChange }: { item: JourneyItem; onChange: () => void }) {
  const p = item.paciente;
  return (
    <Card className={cn(item.severidade === "critico" && "border-critico-border")}>
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={item.severidade} />
          <Badge variant="outline">{RULES[item.regra] ?? item.regra_titulo}</Badge>
          {item.situacao === "a_vencer" && (
            <Badge variant="info">
              <Clock aria-hidden /> A vencer
            </Badge>
          )}
          {item.contexto.risco_origem === "estimado" && <Badge variant="outline">Risco estimado do prontuário</Badge>}
        </div>
        <div>
          <p className="font-bold leading-snug">
            <Link href={`/pacientes/${p.id}`} className="text-primary hover:underline">
              {p.nome}
            </Link>
            {p.idade != null && <span className="font-normal text-muted-foreground"> · {p.idade} anos</span>}
          </p>
          <p className="text-sm font-bold">{item.titulo}</p>
          <p className="text-sm">{item.detalhe}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Desde {fmtDateTime(item.desde)}
            {item.prazo && ` · prazo ${fmtDateTime(item.prazo)}`}
            {p.telefone && ` · ${p.telefone}`}
          </p>
        </div>
        <details className="text-xs text-muted-foreground">
          <summary className="min-h-9 cursor-pointer py-2 font-bold">Por que este paciente está na lista</summary>
          <p className="pb-2">{item.fonte_regra}</p>
        </details>
        <div className="flex flex-wrap items-center gap-1">
          <SourceRefs patientId={p.id} refs={item.fontes.filter((r) => !r.startsWith("Fala/"))} />
          {item.tarefa ? (
            <span className="inline-flex min-h-9 items-center gap-1 text-xs font-bold text-ok">
              <ClipboardList aria-hidden className="size-4" />
              Tarefa #{item.tarefa.id} · {ROLE_LABEL[item.tarefa.papel]} · prazo {fmtDateTime(item.tarefa.prazo)}
            </span>
          ) : (
            <TaskDialog item={item} onDone={onChange} />
          )}
          <ContactDialog patientId={p.id} patientName={p.nome} onDone={onChange} />
          <MessageDialog patientId={p.id} patientName={p.nome} />
        </div>
      </CardContent>
    </Card>
  );
}

function TaskDialog({ item, onDone }: { item: JourneyItem; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<Role>(item.papel_sugerido);
  const [hours, setHours] = useState(String(item.prazo_tarefa_horas));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    const h = Number(hours);
    if (!Number.isInteger(h) || h < 1 || h > 720) {
      setError("Informe o prazo em horas, de 1 a 720.");
      return;
    }
    setBusy(true);
    try {
      await api("/journey/tasks", { method: "POST", json: { patient_id: item.paciente.id, regra: item.regra, papel: role, prazo_horas: h } });
      setOpen(false);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao criar a tarefa");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          <ClipboardList aria-hidden /> Criar tarefa
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Criar tarefa de acompanhamento</DialogTitle>
          <DialogDescription>
            {item.paciente.nome} — {item.titulo}. A tarefa vai para o prontuário (FHIR Task) com as fontes deste item.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor={`t-papel-${item.id}`}>Responsável (por papel)</Label>
            <NativeSelect id={`t-papel-${item.id}`} value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="enfermagem">Enfermagem</option>
              <option value="medico">Médico(a)</option>
              <option value="gestor">Gestão</option>
            </NativeSelect>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`t-prazo-${item.id}`}>Prazo (horas a partir de agora)</Label>
            <Input id={`t-prazo-${item.id}`} type="number" min={1} max={720} value={hours} onChange={(e) => setHours(e.target.value)} />
          </div>
        </div>
        {error && (
          <p role="alert" className="text-sm font-bold text-destructive">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancelar
          </Button>
          <Button disabled={busy} onClick={() => void submit()}>
            Criar tarefa
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------- tarefas ----------
function Tasks() {
  const [status, setStatus] = useState("aberta");
  const { data, error, loading, reload } = useApi<JourneyTask[]>(`/journey/tasks?status=${status}`);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="f-status">Situação</Label>
          <NativeSelect id="f-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="aberta">Abertas</option>
            <option value="concluida">Concluídas</option>
            <option value="cancelada">Canceladas</option>
          </NativeSelect>
        </div>
        <Button variant="outline" onClick={() => void reload()} disabled={loading}>
          <RefreshCw aria-hidden /> Atualizar
        </Button>
      </div>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-32 w-full" />}
      {data && data.length === 0 && <p className="text-sm text-muted-foreground">Nenhuma tarefa nesta situação.</p>}
      {data && data.length > 0 && (
        <ul className="space-y-2" aria-label="Tarefas de acompanhamento">
          {data.map((t) => (
            <li key={t.id}>
              <Card>
                <CardContent className="space-y-2 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{RULES[t.regra] ?? t.regra}</Badge>
                    <Badge variant="secondary">{ROLE_LABEL[t.papel]}</Badge>
                    <span className="text-xs text-muted-foreground">
                      Tarefa #{t.id} · prazo {fmtDateTime(t.prazo)} · {t.fhir_ref}
                    </span>
                  </div>
                  <p className="font-bold leading-snug">
                    {t.paciente && (
                      <Link href={`/pacientes/${t.paciente.id}`} className="text-primary hover:underline">
                        {t.paciente.nome}
                      </Link>
                    )}{" "}
                    — {t.titulo}
                  </p>
                  <p className="text-sm">{t.detalhe}</p>
                  {t.observacao_encerramento && <p className="text-xs text-muted-foreground">Encerramento: {t.observacao_encerramento}</p>}
                  {t.status === "aberta" && (
                    <div className="flex flex-wrap gap-2">
                      <CloseTask task={t} status="concluida" onDone={() => void reload()} />
                      <CloseTask task={t} status="cancelada" onDone={() => void reload()} />
                    </div>
                  )}
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CloseTask({ task, status, onDone }: { task: JourneyTask; status: "concluida" | "cancelada"; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const done = status === "concluida";

  async function submit() {
    if (!done && note.trim().length < 10) {
      setError("Descreva o motivo (mín. 10 caracteres).");
      return;
    }
    try {
      await api(`/journey/tasks/${task.id}`, { method: "PATCH", json: { status, observacao: note.trim() || null } });
      setOpen(false);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao encerrar a tarefa");
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant={done ? "default" : "ghost"}>
          {done ? "Concluir" : "Cancelar tarefa"}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{done ? "Concluir tarefa" : "Cancelar tarefa"}</DialogTitle>
          <DialogDescription>{task.titulo}</DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor={`c-${task.id}-${status}`}>{done ? "O que foi feito (opcional)" : "Motivo do cancelamento"}</Label>
          <Textarea id={`c-${task.id}-${status}`} value={note} onChange={(e) => setNote(e.target.value)} aria-invalid={error ? true : undefined} />
          {error && (
            <p role="alert" className="text-xs font-bold text-destructive">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => setOpen(false)}>
            Voltar
          </Button>
          <Button variant={done ? "default" : "destructive"} onClick={() => void submit()}>
            {done ? "Concluir tarefa" : "Cancelar tarefa"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
