"use client";

import { useCallback, useEffect, useState } from "react";
import { History, Loader2, Mic, PlusCircle, ShieldCheck, ShieldOff, Volume2 } from "lucide-react";

import { LiveConsult } from "@/components/consult/live-consult";
import { NoteReview } from "@/components/consult/note-review";
import { SmartAppsPanel } from "@/components/consult/smart-apps";
import { SurgicalPanel } from "@/components/consult/surgical-panel";
import { Transcript, type SegmentPatch } from "@/components/consult/transcript";
import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type ConsentPurposes, type ConsultConfig, type ConsultKind, type Consultation } from "@/lib/api";
import { cn } from "@/lib/utils";
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
  // idioma preferido do paciente no cadastro (Patient.communication): pré-seleciona a tradução, o médico pode trocar
  const preferred = useApi<{ idioma: string; rotulo: string; fonte: string }>(allowed ? `/patients/${patient.id}/consult/idioma` : null);
  const [langTouched, setLangTouched] = useState(false);
  useEffect(() => {
    if (preferred.data && !langTouched) setIdioma(preferred.data.idioma);
  }, [preferred.data, langTouched]);
  // consentimento (etapa obrigatória antes de Iniciar): quem consentiu, finalidades e recusa
  const companions = useApi<{ id: string; nome: string; parentesco: string }[]>(allowed ? `/patients/${patient.id}/consult/acompanhantes` : null);
  const [quem, setQuem] = useState<"paciente" | "acompanhante" | "responsavel">("paciente");
  const [relatedId, setRelatedId] = useState("");
  const [finalidades, setFinalidades] = useState<ConsentPurposes>({ transcricao: true, ia_externa: true, traducao: true, emocoes: true });
  const [recusou, setRecusou] = useState(false);
  const [avisoLido, setAvisoLido] = useState(false);
  const [tab, setTab] = useState<"nota" | "transcricao">("nota");
  const proLabel = user?.role === "enfermagem" ? "Enfermagem" : "Médico(a)";
  // no modo cirúrgico o seletor mostra só as especialidades com modelo de relatório (ortopedia, obstetrícia…)
  const surgicalMode = tipo === "cirurgia";
  const specialtyOptions = (config.data?.especialidades ?? []).filter((s) => !surgicalMode || s.cirurgica);
  const chosenSpecialty = specialtyOptions.some((s) => s.value === especialidade)
    ? especialidade
    : surgicalMode
      ? "cirurgia_geral"
      : (specialtyOptions[0]?.value ?? "clinica_medica");

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
        json: {
          tipo,
          especialidade: chosenSpecialty,
          idioma_paciente: surgicalMode ? "pt-BR" : idioma,
          consentimento: {
            quem,
            related_person_id: quem !== "paciente" && relatedId ? relatedId : null,
            finalidades,
            recusou,
          },
        },
      });
      setAvisoLido(false);
      setRecusou(false);
      setCurrent(c);
      void history.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao iniciar");
    } finally {
      setOpening(false);
    }
  }

  const patientLang = surgicalMode ? "pt-BR" : idioma;
  const notice = config.data?.aviso_consentimento?.[patientLang] ?? config.data?.aviso_consentimento?.["pt-BR"] ?? "";
  function readAloud() {
    const u = new SpeechSynthesisUtterance(notice);
    u.lang = config.data?.idiomas.find((i) => i.value === patientLang)?.web_speech ?? patientLang;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  // "fala N" na nota revisada leva até a fala na transcrição (no tablet, troca para a aba da transcrição antes)
  useEffect(() => {
    const onFala = (e: Event) => {
      if (tab === "transcricao" || window.matchMedia("(min-width: 1024px)").matches) return;
      const n = (e as CustomEvent<number>).detail;
      setTab("transcricao");
      window.setTimeout(() => window.dispatchEvent(new CustomEvent("housia:fala", { detail: n })), 80);
    };
    window.addEventListener("housia:fala", onFala);
    return () => window.removeEventListener("housia:fala", onFala);
  }, [tab]);

  const editSegment = useCallback(
    async (n: number, patch: SegmentPatch) => {
      if (!current) return;
      setCurrent(await api<Consultation>(`/consultations/${current.id}/segments/${n}`, { method: "PATCH", json: patch }));
    },
    [current],
  );

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
        <div role="tablist" aria-label="Nota ou transcrição" className="flex gap-1 rounded-lg bg-muted p-1 lg:hidden">
          {(["nota", "transcricao"] as const).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={cn(
                "min-h-10 flex-1 rounded-md px-3 text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                tab === k ? "bg-card text-primary shadow-sm" : "text-muted-foreground",
              )}
            >
              {k === "nota" ? "Nota" : `Transcrição (${current.segmentos.length})`}
            </button>
          ))}
        </div>
        <div className="grid gap-4 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
          <div className={cn(tab === "nota" ? "block" : "hidden", "lg:block")}>
            <NoteReview consultation={current} onChange={setCurrent} canSend={config.data.envio_prontuario} />
          </div>
          <div className={cn(tab === "transcricao" ? "block" : "hidden", "space-y-3 self-start lg:block")}>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Transcrição</CardTitle>
                <CardDescription>
                  {current.status === "enviada"
                    ? "Nota já enviada: a transcrição não pode mais ser editada."
                    : "Corrija o texto ou quem falou; as regras rodam de novo e a nota fica marcada como desatualizada."}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Transcript
                  segments={current.segmentos}
                  interim={null}
                  proLabel={proLabel}
                  patientLanguage={current.idioma_paciente}
                  onEdit={current.status === "enviada" ? undefined : editSegment}
                  speakers={current.tipo === "cirurgia" ? ["medico", "paciente", "acompanhante", "anestesista", "circulante", "instrumentador"] : undefined}
                  maxHeight="max-h-[70vh]"
                />
              </CardContent>
            </Card>
            <details className="rounded-lg border bg-card p-3" open={false}>
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
              <Label htmlFor="especialidade">Especialidade</Label>
              <NativeSelect id="especialidade" value={chosenSpecialty} onChange={(e) => setEspecialidade(e.target.value)}>
                {specialtyOptions.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="idioma">Idioma do paciente</Label>
              <NativeSelect id="idioma" value={idioma} onChange={(e) => {
                  setLangTouched(true);
                  setIdioma(e.target.value);
                }}
                disabled={tipo === "cirurgia"}
              >
                {config.data.idiomas.map((i) => (
                  <option key={i.value} value={i.value}>
                    {i.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            Foco do assistente: {config.data.especialidades.find((s) => s.value === chosenSpecialty)?.foco}.
            {surgicalMode &&
              " O modelo do relatório segue o procedimento ditado ou agendado (ex.: artroplastia de joelho, cesariana); a especialidade vale quando nenhum dos dois é reconhecido."}
          </p>
          {preferred.data?.fonte && preferred.data.idioma !== "pt-BR" && (
            <p className="text-sm text-muted-foreground" data-testid="idioma-cadastro">
              Idioma sugerido pelo cadastro do paciente ({preferred.data.fonte}): {preferred.data.rotulo}.
            </p>
          )}
          {idioma !== "pt-BR" && (
            <p className="text-sm text-muted-foreground">
              Tradução simultânea: as falas do paciente aparecem traduzidas para você e as suas, no idioma do paciente (com áudio).
              {config.data.llm_mode === "mock" && " No modo demonstração a tradução fica marcada como pendente do LLM real."}
            </p>
          )}
          <fieldset className="space-y-3 rounded-lg border bg-card p-3" data-testid="consentimento">
            <legend className="flex items-center gap-1.5 px-1 font-bold">
              <ShieldCheck aria-hidden className="size-4 text-primary" /> Consentimento do paciente
            </legend>
            <p className="rounded-md bg-info-bg p-2 text-sm" lang={patientLang}>
              {notice}
            </p>
            <Button type="button" variant="outline" size="sm" onClick={readAloud}>
              <Volume2 aria-hidden /> Ler em voz alta
            </Button>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="quem-consentiu">Quem consentiu</Label>
                <NativeSelect id="quem-consentiu" value={quem} onChange={(e) => setQuem(e.target.value as typeof quem)}>
                  <option value="paciente">O próprio paciente</option>
                  <option value="acompanhante">Acompanhante</option>
                  <option value="responsavel">Responsável legal</option>
                </NativeSelect>
              </div>
              {quem !== "paciente" && (
                <div className="space-y-1.5">
                  <Label htmlFor="related-person">Pessoa no prontuário</Label>
                  <NativeSelect id="related-person" value={relatedId} onChange={(e) => setRelatedId(e.target.value)}>
                    <option value="">Não listada</option>
                    {(companions.data ?? []).map((rp) => (
                      <option key={rp.id} value={rp.id}>
                        {rp.nome}
                        {rp.parentesco ? ` (${rp.parentesco})` : ""}
                      </option>
                    ))}
                  </NativeSelect>
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Checkbox id="recusou-ia" checked={recusou} onCheckedChange={(v) => setRecusou(v === true)} />
              <Label htmlFor="recusou-ia">O paciente recusou a IA (a consulta segue só com as regras, sem IA externa)</Label>
            </div>
            <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label="Finalidades autorizadas">
              {(config.data.finalidades ?? []).map((f) => (
                <div key={f.value} className="flex items-center gap-2">
                  <Checkbox
                    id={`fin-${f.value}`}
                    checked={!recusou && finalidades[f.value]}
                    disabled={recusou}
                    onCheckedChange={(v) => setFinalidades((prev) => ({ ...prev, [f.value]: v === true }))}
                  />
                  <Label htmlFor={`fin-${f.value}`}>{f.label}</Label>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-2 border-t pt-2">
              <Checkbox id="aviso-lido" checked={avisoLido} onCheckedChange={(v) => setAvisoLido(v === true)} />
              <Label htmlFor="aviso-lido" className="font-bold">
                Li o aviso ao paciente e registrei a decisão dele
              </Label>
            </div>
          </fieldset>
          {error && <Alert variant="critico">{error}</Alert>}
          <Button size="lg" onClick={() => void start()} disabled={opening || !avisoLido}>
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
