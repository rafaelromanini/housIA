"use client";

import { useCallback, useEffect, useState } from "react";
import { History, Loader2, Mic, PlusCircle, ShieldOff } from "lucide-react";

import { LiveConsult } from "@/components/consult/live-consult";
import { NoteReview } from "@/components/consult/note-review";
import { SmartAppsPanel } from "@/components/consult/smart-apps";
import { SurgicalPanel } from "@/components/consult/surgical-panel";
import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type ConsultConfig, type ConsultKind, type Consultation } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";

type Summary = { id: number; tipo: ConsultKind; especialidade: string; status: Consultation["status"]; falas: number; created_at: string };

const STATUS = {
  em_andamento: { label: "Em andamento", variant: "atencao" },
  finalizada: { label: "Nota para revisar", variant: "info" },
  enviada: { label: "No prontuário", variant: "ok" },
} as const;

export function ConsultPage() {
  const { patient } = usePatient();
  const { user } = useAuth();
  const allowed = user?.role === "medico" || user?.role === "enfermagem";
  const config = useApi<ConsultConfig>(allowed ? "/consult/config" : null);
  const history = useApi<Summary[]>(allowed ? `/patients/${patient.id}/consultations` : null);
  const [current, setCurrent] = useState<Consultation | null>(null);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tipo, setTipo] = useState<ConsultKind>(patient.internado ? "visita" : "consulta");
  const [especialidade, setEspecialidade] = useState(user?.role === "enfermagem" ? "enfermagem" : "clinica_medica");
  const [idioma, setIdioma] = useState("pt-BR");
  const proLabel = user?.role === "enfermagem" ? "Enfermagem" : "Médico(a)";

  const open = useCallback(async (id: number) => {
    setOpening(true);
    try {
      setCurrent(await api<Consultation>(`/consultations/${id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao abrir a consulta");
    } finally {
      setOpening(false);
    }
  }, []);

  // ao abrir a página, retoma a consulta em andamento deste paciente (uma vez só)
  const [resumed, setResumed] = useState(false);
  useEffect(() => {
    if (resumed || !history.data) return;
    setResumed(true);
    const running = history.data.find((h) => h.status === "em_andamento");
    if (running) void open(running.id);
  }, [history.data, resumed, open]);

  async function start() {
    setOpening(true);
    setError(null);
    try {
      const c = await api<Consultation>(`/patients/${patient.id}/consultations`, {
        method: "POST",
        json: { tipo, especialidade: tipo === "cirurgia" ? "cirurgia_geral" : especialidade, idioma_paciente: idioma },
      });
      setCurrent(c);
      void history.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao iniciar");
    } finally {
      setOpening(false);
    }
  }

  if (!allowed) {
    return (
      <Alert variant="atencao">
        <ShieldOff aria-hidden /> A consulta com IA é usada por médicos(as) e enfermagem.
      </Alert>
    );
  }
  if (!config.data) return <Skeleton className="h-96 w-full" />;

  if (current?.status === "em_andamento") {
    return <LiveConsult consultation={current} setConsultation={setCurrent} config={config.data} proLabel={proLabel} />;
  }

  if (current) {
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground">
            Consulta de {fmtDateTime(current.created_at)} · {current.segmentos.length} falas · {current.especialidade_rotulo}
          </p>
          <Button
            variant="outline"
            onClick={() => {
              setCurrent(null);
              void history.reload();
            }}
          >
            <PlusCircle aria-hidden /> Nova consulta
          </Button>
        </div>
        <div className="grid gap-4 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
          <NoteReview consultation={current} onChange={setCurrent} canSend={config.data.envio_prontuario} />
          <details className="self-start rounded-lg border bg-card p-3" open={false}>
            <summary className="min-h-9 cursor-pointer font-bold">Smart Apps desta consulta</summary>
            <div className="mt-2">
              {current.insights?.relatorio_cirurgico ? (
                <SurgicalPanel report={current.insights.relatorio_cirurgico} patientId={patient.id} />
              ) : current.insights ? (
                <SmartAppsPanel insights={current.insights} />
              ) : null}
            </div>
          </details>
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Mic aria-hidden className="size-5 text-primary" /> Consulta com IA
          </CardTitle>
          <CardDescription>
            Transcreve a conversa em tempo real, sugere perguntas enquanto você atende, organiza a anamnese, cruza com o prontuário e
            gera a nota para você revisar e enviar.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="tipo">Atendimento</Label>
              <NativeSelect id="tipo" value={tipo} onChange={(e) => setTipo(e.target.value as ConsultKind)}>
                <option value="consulta">Consulta</option>
                <option value="visita">Visita à beira do leito</option>
                <option value="cirurgia">Relatório cirúrgico (ditado)</option>
              </NativeSelect>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="especialidade">Assistente da especialidade</Label>
              <NativeSelect
                id="especialidade"
                value={tipo === "cirurgia" ? "cirurgia_geral" : especialidade}
                disabled={tipo === "cirurgia"}
                onChange={(e) => setEspecialidade(e.target.value)}
              >
                {config.data.especialidades.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="idioma">Idioma do paciente</Label>
              <NativeSelect id="idioma" value={idioma} onChange={(e) => setIdioma(e.target.value)} disabled={tipo === "cirurgia"}>
                {config.data.idiomas.map((i) => (
                  <option key={i.value} value={i.value}>
                    {i.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            Foco do assistente: {config.data.especialidades.find((s) => s.value === (tipo === "cirurgia" ? "cirurgia_geral" : especialidade))?.foco}.
          </p>
          {idioma !== "pt-BR" && (
            <p className="text-sm text-muted-foreground">
              Tradução simultânea: as falas do paciente aparecem traduzidas para você e as suas, no idioma do paciente (com áudio).
              {config.data.llm_mode === "mock" && " No modo demonstração a tradução fica marcada como pendente do LLM real."}
            </p>
          )}
          <Alert variant="info">
            Avise o paciente que a conversa será transcrita para apoiar o atendimento. O HousIA não grava áudio; o texto fica no
            prontuário do HousIA e identificadores são retirados antes de ir para a IA.
          </Alert>
          {error && <Alert variant="critico">{error}</Alert>}
          <Button size="lg" onClick={() => void start()} disabled={opening}>
            {opening ? <Loader2 aria-hidden className="animate-spin" /> : <Mic aria-hidden />} Iniciar
          </Button>
        </CardContent>
      </Card>

      <Card className="self-start">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <History aria-hidden className="size-5 text-primary" /> Consultas anteriores
          </CardTitle>
        </CardHeader>
        <CardContent>
          {!history.data ? (
            <Skeleton className="h-20" />
          ) : history.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nenhuma consulta com IA ainda.</p>
          ) : (
            <ul className="space-y-1">
              {history.data.map((h) => (
                <li key={h.id}>
                  <button
                    className="flex min-h-11 w-full flex-wrap items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-secondary"
                    onClick={() => void open(h.id)}
                  >
                    <span className="font-bold">{fmtDateTime(h.created_at)}</span>
                    <span className="text-muted-foreground">
                      {h.especialidade} · {h.falas} falas
                    </span>
                    <Badge variant={STATUS[h.status].variant} className="ml-auto">
                      {STATUS[h.status].label}
                    </Badge>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
