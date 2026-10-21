"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  FileCheck2,
  Keyboard,
  Loader2,
  Mic,
  MicOff,
  Pause,
  Play,
  ShieldOff,
  ShieldPlus,
  Radio,
  RefreshCw,
  Send,
  Server,
  Sparkles,
} from "lucide-react";

import {
  useDemoPlayer,
  useMicrofones,
  usePushToTalk,
  useServerRecorder,
  useSpeechCapture,
  useTesteDeAudio,
  useWakeLock,
  speechSupported,
} from "@/components/consult/capture";
import { CopilotPanel } from "@/components/consult/copilot-panel";
import { PrivacyPanel } from "@/components/consult/privacy-panel";
import { SmartAppsPanel } from "@/components/consult/smart-apps";
import { SurgicalPanel } from "@/components/consult/surgical-panel";
import { Transcript, clock, speakerLabel, SPEAKER_STYLE, TEAM_SPEAKERS, type SegmentPatch } from "@/components/consult/transcript";
import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import {
  api,
  type ChecklistStep,
  type ConsultConfig,
  type ConsultInsights,
  type Consultation,
  type DemoScript,
  type HypothesisDecision,
  type InsightAction,
  type Segment,
  type Speaker,
} from "@/lib/api";
import {
  carregarFila,
  enfileirar,
  type FalaPendente,
  limparFila,
  novoClientId,
  pendentes as filaPendentes,
  removerEnviadas,
} from "@/lib/offline-queue";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

type Mode = "microfone" | "servidor" | "digitar" | "demo";
type SegmentIn = { falante: Speaker; texto: string; t?: number; origem: Segment["origem"]; idioma?: string; client_id?: string };

const AI_EVERY_MS = 12000;
const KIND_LABEL = { consulta: "Consulta", visita: "Visita à beira do leito", cirurgia: "Ditado cirúrgico" };
const CONSULT_SPEAKERS: Speaker[] = ["medico", "paciente", "acompanhante", "desconhecido"];
// modo cirúrgico: a equipe de sala também dita (Alt+4 anestesista, Alt+5 circulante, Alt+6 instrumentador)
const SURGICAL_SPEAKERS: Speaker[] = ["medico", "paciente", "acompanhante", ...TEAM_SPEAKERS, "desconhecido"];

function mergeSegments(prev: Segment[], incoming: Segment[]) {
  const byN = new Map(prev.map((s) => [s.n, s]));
  incoming.forEach((s) => byN.set(s.n, { ...byN.get(s.n), ...s }));
  return [...byN.values()].sort((a, b) => a.n - b.n);
}

function newerInsights(prev: ConsultInsights | null, next: ConsultInsights | null) {
  if (!next) return prev;
  if (!prev) return next;
  return next.falas_analisadas >= prev.falas_analisadas ? next : prev;
}

export function LiveConsult({
  consultation: c,
  setConsultation,
  config,
  proLabel: consultLabel,
}: {
  consultation: Consultation;
  setConsultation: React.Dispatch<React.SetStateAction<Consultation | null>>;
  config: ConsultConfig;
  proLabel: string;
}) {
  const { patient } = usePatient();
  const surgical = c.tipo === "cirurgia";
  const proLabel = surgical ? "Cirurgião(ã)" : consultLabel;
  const speakers = surgical ? SURGICAL_SPEAKERS : CONSULT_SPEAKERS;
  const browserStt = config.stt_navegador;
  const [mode, setMode] = useState<Mode>(() =>
    c.roteiro_demo
      ? "demo"
      : browserStt && speechSupported()
        ? "microfone"
        : config.stt_servidor // iOS/Safari sem Web Speech: usa o STT do servidor em vez de pedir Chrome ou Edge
          ? "servidor"
          : "digitar",
  );
  // no microfone a fala chega contínua: começa em "Automático" (as regras atribuem quem falou e a IA corrige). Com
  // paciente em outro idioma, quem fala escolhe o idioma do reconhecimento, então o falante continua manual. No ditado
  // cirúrgico quem dita é a equipe: começa em "Profissional" (a heurística chamaria a afirmação solta de paciente).
  const startSpeaker = useCallback(
    (m: Mode): Speaker => (m === "microfone" && !surgical && c.idioma_paciente === "pt-BR" ? "desconhecido" : "medico"),
    [c.idioma_paciente, surgical],
  );
  const [speaker, setSpeaker] = useState<Speaker>(() => startSpeaker(mode));
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [aiRunning, setAiRunning] = useState(false);
  const [autoAI, setAutoAI] = useState(true);
  const [autoVoice, setAutoVoice] = useState(true);
  const autoVoiceRef = useRef(true);
  autoVoiceRef.current = autoVoice;
  const [finishing, setFinishing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const cRef = useRef(c);
  cRef.current = c;
  const lastAi = useRef(0);
  const aiBusy = useRef(false);
  // falas aceitas pela captura e ainda não confirmadas pelo servidor: se o envio falhar, vão no corpo do /finish
  const unsent = useRef<SegmentIn[]>([]);
  const consent = c.consentimento;
  const consentState = consent?.estado ?? "ativo";
  const canAI = consent ? consent.permite.ia : true;
  const captureBlocked = consentState !== "ativo";
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  useEffect(() => {
    const started = new Date(c.created_at).getTime();
    const id = window.setInterval(() => setElapsed((Date.now() - started) / 1000), 1000);
    return () => window.clearInterval(id);
  }, [c.created_at]);

  const apply = useCallback(
    (novos: Segment[], insights: ConsultInsights | null) =>
      setConsultation((prev) =>
        prev ? { ...prev, segmentos: mergeSegments(prev.segmentos, novos), insights: newerInsights(prev.insights, insights) } : prev,
      ),
    [setConsultation],
  );

  const translate = useCallback(
    async (ns: number[]) => {
      const allowed = cRef.current.consentimento ? cRef.current.consentimento.permite.traducao : true;
      const foreign = cRef.current.segmentos.some((s) => ns.includes(s.n) && s.idioma !== "pt-BR");
      if ((cRef.current.idioma_paciente === "pt-BR" && !foreign) || ns.length === 0 || !allowed) return;
      try {
        const out = await api<{ traducoes: Record<string, string>; divergentes: Record<string, string[]>; insights: ConsultInsights | null }>(
          `/consultations/${c.id}/translate`,
          { method: "POST", json: { ns } },
        );
        setConsultation((prev) =>
          prev
            ? {
                ...prev,
                segmentos: prev.segmentos.map((s) =>
                  out.traducoes[s.n] ? { ...s, traducao: out.traducoes[s.n], traducao_divergente: out.divergentes?.[s.n] ?? [] } : s,
                ),
                insights: newerInsights(prev.insights, out.insights),
              }
            : prev,
        );
        // voz automática: a fala do profissional traduzida é lida em voz alta no idioma do paciente (desligável)
        if (autoVoiceRef.current && typeof window !== "undefined" && "speechSynthesis" in window) {
          const voice = config.idiomas.find((i) => i.value === cRef.current.idioma_paciente)?.web_speech ?? cRef.current.idioma_paciente;
          cRef.current.segmentos
            .filter((s) => ns.includes(s.n) && s.falante === "medico" && out.traducoes[s.n] && !out.traducoes[s.n].includes("tradução com LLM real"))
            .forEach((s) => {
              const u = new SpeechSynthesisUtterance(reidentify(out.traducoes[s.n], patient.nome));
              u.lang = voice;
              window.speechSynthesis.speak(u);
            });
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro na tradução");
      }
    },
    [c.id, setConsultation, config.idiomas, patient.nome],
  );

  // ---------- fila offline (#12): só texto, com client_id; reenvio em ordem quando a rede volta ----------
  const [aguardando, setAguardando] = useState(0);
  const filaParada = useRef(false); // erro do servidor no reenvio: só tenta de novo por ação da pessoa ou ao voltar a rede
  const postSegments = useCallback(
    async (items: SegmentIn[]) => {
      const res = await api<{ novos: Segment[]; insights: ConsultInsights }>(`/consultations/${c.id}/segments`, {
        method: "POST",
        json: { segmentos: items },
      });
      apply(res.novos, res.insights);
      void translate(res.novos.map((s) => s.n));
    },
    [apply, c.id, translate],
  );
  const dropUnsent = (ids: Set<string | undefined>) => {
    unsent.current = unsent.current.filter((u) => !ids.has(u.client_id));
  };

  const flushOffline = useCallback(() => {
    queue.current = queue.current.then(async () => {
      for (;;) {
        const lote = filaPendentes(c.id).slice(0, 50);
        if (lote.length === 0) break;
        try {
          await postSegments(lote as SegmentIn[]);
          await removerEnviadas(c.id, lote.map((f) => f.client_id));
          dropUnsent(new Set(lote.map((f) => f.client_id)));
          setAguardando(filaPendentes(c.id).length);
          setError(null);
        } catch (e) {
          if (!(e instanceof TypeError)) {
            filaParada.current = true;
            setError(e instanceof Error ? `${e.message} As falas pendentes continuam guardadas.` : "Erro ao reenviar as falas pendentes");
          }
          break; // rede ainda fora (ou erro do servidor): mantém a fila e a ordem
        }
      }
    });
  }, [c.id, postSegments]);

  // envia em ordem (fila), mesmo com falas chegando rápido; sem rede, a fala vai para a fila offline
  const send = useCallback(
    (items: SegmentIn[]) => {
      const withId = items.map((i) => (i.client_id ? i : { ...i, client_id: novoClientId() }));
      unsent.current.push(...withId);
      queue.current = queue.current.then(async () => {
        const toQueue = async () => {
          await enfileirar(c.id, withId as FalaPendente[]);
          setAguardando(filaPendentes(c.id).length);
        };
        if (filaPendentes(c.id).length > 0) {
          await toQueue(); // já há falas esperando: esta entra atrás delas para manter a ordem
          if (!filaParada.current && navigator.onLine) flushOffline();
          return;
        }
        try {
          await postSegments(withId);
          dropUnsent(new Set(withId.map((i) => i.client_id)));
        } catch (e) {
          if (e instanceof TypeError) await toQueue();
          else setError(e instanceof Error ? e.message : "Erro ao enviar a fala");
        }
      });
    },
    [c.id, flushOffline, postSegments],
  );

  // retoma o que ficou pendente (a aba pode ter sido recarregada sem rede) e reenvia quando a rede volta
  useEffect(() => {
    let vivo = true;
    void carregarFila(c.id).then((f) => {
      if (!vivo) return;
      setAguardando(f.length);
      if (f.length > 0 && navigator.onLine) flushOffline();
    });
    const online = () => {
      filaParada.current = false;
      flushOffline();
    };
    window.addEventListener("online", online);
    return () => {
      vivo = false;
      window.removeEventListener("online", online);
    };
  }, [c.id, flushOffline]);

  useEffect(() => {
    if (aguardando === 0) return;
    const id = window.setInterval(() => {
      if (!filaParada.current) flushOffline();
    }, 5000);
    return () => window.clearInterval(id);
  }, [aguardando, flushOffline]);

  const runAI = useCallback(
    async (force = false) => {
      if (aiBusy.current || !(cRef.current.consentimento?.permite.ia ?? true)) return;
      aiBusy.current = true;
      setAiRunning(true);
      lastAi.current = Date.now();
      try {
        const updated = await api<Consultation>(`/consultations/${c.id}/insights${force ? "?force=true" : ""}`, { method: "POST" });
        setConsultation((prev) =>
          prev
            ? { ...prev, segmentos: mergeSegments(prev.segmentos, updated.segmentos), insights: newerInsights(prev.insights, updated.insights), ia: updated.ia }
            : prev,
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro na análise da IA");
      } finally {
        aiBusy.current = false;
        setAiRunning(false);
      }
    },
    [c.id, setConsultation],
  );

  // ciclos da IA: quando há falas novas, no máximo a cada 12 s (as regras já atualizam a cada fala)
  useEffect(() => {
    if (!autoAI || surgical || !canAI) return;
    const id = window.setInterval(() => {
      const cur = cRef.current;
      if (cur.segmentos.length > (cur.ia?.falas ?? 0) && Date.now() - lastAi.current > AI_EVERY_MS) void runAI();
    }, 3000);
    return () => window.clearInterval(id);
  }, [autoAI, runAI, surgical, canAI]);

  // ---------- captura ----------
  const patientSide = speaker === "paciente" || speaker === "acompanhante";
  const lang = patientSide ? (config.idiomas.find((i) => i.value === c.idioma_paciente)?.web_speech ?? c.idioma_paciente) : "pt-BR";
  const mic = useSpeechCapture(lang, (texto) => send([{ falante: speaker, texto, origem: "microfone" }]));
  const mics = useMicrofones();
  const recorder = useServerRecorder((blob) => {
    queue.current = queue.current.then(async () => {
      try {
        const res = await api<{ novos: Segment[]; insights: ConsultInsights }>(`/consultations/${c.id}/audio?falante=${speaker}`, {
          method: "POST",
          body: blob,
          headers: { "Content-Type": blob.type || "audio/webm" },
        });
        apply(res.novos, res.insights);
        void translate(res.novos.map((s) => s.n));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro na transcrição do servidor");
      }
    });
  }, 8, mics.deviceId);
  const audioTest = useTesteDeAudio(mics.deviceId, mics.refresh);
  const script = useApi<DemoScript>(c.roteiro_demo && mode === "demo" ? `/consult/demo-scripts/${c.roteiro_demo}` : null);
  const demoStart = c.segmentos.filter((s) => s.origem === "demo").length;
  const demo = useDemoPlayer(script.data, demoStart, (line) => send([{ falante: line.falante, texto: line.texto, t: line.t, origem: "demo", idioma: line.idioma }]));

  // Alt+Q (segurar): pergunta por voz ao copiloto. Pausa a transcrição enquanto fala e nada vira Fala/N
  const resumeMic = useRef(false);
  const [pushed, setPushed] = useState<{ texto: string; n: number } | null>(null);
  const ptt = usePushToTalk(
    (texto) => setPushed((p) => ({ texto, n: (p?.n ?? 0) + 1 })),
    () => {
      resumeMic.current = mic.listening;
      if (mic.listening) void mic.stop();
    },
    () => {
      if (resumeMic.current) mic.start();
      resumeMic.current = false;
    },
  );
  const pttStart = ptt.start;
  const pttStop = ptt.stop;

  const capturing = mic.listening || recorder.recording || demo.playing;
  const wakeLock = useWakeLock(capturing);
  const interim = demo.interim ?? (mic.interim ? { falante: speaker, texto: mic.interim } : null);

  /** Para toda captura e espera o último resultado do reconhecimento (ou o último trecho de áudio) entrar na fila. */
  async function stopAll() {
    demo.pause();
    await Promise.all([mic.stop(), recorder.stop()]);
  }

  // atalhos: Alt+1 profissional, Alt+2 paciente, Alt+3 acompanhante, Alt+0 detectar automaticamente;
  // no centro cirúrgico também Alt+4 anestesista, Alt+5 circulante e Alt+6 instrumentador
  useEffect(() => {
    if (surgical || !browserStt) return; // sem STT do navegador (ALLOW_BROWSER_STT=false) o atalho de voz não liga o microfone
    const down = (e: KeyboardEvent) => {
      if (e.altKey && (e.code === "KeyQ" || e.key.toLowerCase() === "q") && !e.repeat) {
        e.preventDefault();
        pttStart();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "q" || e.key === "Alt") pttStop();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [surgical, browserStt, pttStart, pttStop]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey) return;
      const map: Record<string, Speaker> = { "1": "medico", "2": "paciente", "3": "acompanhante", "0": "desconhecido" };
      if (surgical) Object.assign(map, { "4": "anestesista", "5": "circulante", "6": "instrumentador" });
      if (map[e.key]) {
        e.preventDefault();
        setSpeaker(map[e.key]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [surgical]);

  // guia de OPME, etiquetas e convênio mudam o relatório: o servidor devolve os insights atualizados
  const refreshInsights = useCallback(
    (insights: ConsultInsights | null, patch?: Partial<Consultation>) => {
      apply([], insights);
      if (patch) setConsultation((prev) => (prev ? { ...prev, ...patch } : prev));
    },
    [apply, setConsultation],
  );

  // Cirurgia Segura (OMS) marcada por botão; o ditado também marca por voz
  const markChecklist = useCallback(
    async (etapa: ChecklistStep, feito: boolean) => {
      await queue.current;
      try {
        const res = await api<{ insights: ConsultInsights }>(`/consultations/${c.id}/checklist`, { method: "POST", json: { etapa, feito } });
        apply([], res.insights);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro ao marcar a etapa");
      }
    },
    [apply, c.id],
  );

  // o profissional fixa (ou solta) a queixa principal da anamnese
  const setMainComplaint = useCallback(
    async (chave: string | null) => {
      await queue.current;
      try {
        const res = await api<{ insights: ConsultInsights }>(`/consultations/${c.id}/queixa-principal`, { method: "PUT", json: { chave } });
        apply([], res.insights);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro ao fixar a queixa principal");
      }
    },
    [apply, c.id],
  );

  // o profissional confirma, deixa provisória ou descarta uma hipótese diagnóstica
  const decideHypothesis = useCallback(
    async (cidCode: string, decisao: HypothesisDecision, motivo?: string) => {
      await queue.current;
      try {
        const res = await api<{ insights: ConsultInsights }>(`/consultations/${c.id}/hipoteses`, {
          method: "PUT",
          json: { cid: cidCode, decisao, motivo: motivo ?? "" },
        });
        apply([], res.insights);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro ao registrar a decisão sobre a hipótese");
      }
    },
    [apply, c.id],
  );

  // aceitar, descartar (com motivo), marcar como feita ou fixar um insight; o item descartado não volta nas próximas falas
  const decideInsight = useCallback(
    async (itemKey: string, acao: InsightAction, motivo?: string) => {
      await queue.current;
      try {
        const res = await api<{ insights: ConsultInsights }>(`/consultations/${c.id}/insight-actions`, {
          method: "POST",
          json: { item_key: itemKey, acao, motivo: motivo ?? "" },
        });
        apply([], res.insights);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro ao registrar a decisão sobre o item");
      }
    },
    [apply, c.id],
  );

  async function finish() {
    setFinishing(true);
    setError(null);
    // espera o último resultado do reconhecimento / trecho de áudio e a fila de envio; o que não chegou ao servidor
    // (falha de rede, por exemplo) vai no corpo do /finish e entra de forma atômica antes da nota
    await stopAll();
    await queue.current;
    if (filaPendentes(c.id).length > 0 && navigator.onLine) {
      flushOffline();
      await queue.current;
    }
    try {
      const updated = await api<Consultation>(`/consultations/${c.id}/finish`, {
        method: "POST",
        json: { segmentos: unsent.current },
      });
      unsent.current = [];
      await limparFila(c.id); // a nota já tem as falas: nada de texto de paciente fica no aparelho
      setAguardando(0);
      setConsultation(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao gerar a nota");
    } finally {
      setFinishing(false);
    }
  }

  // correção de uma fala (texto e/ou quem falou): o servidor reroda as regras e devolve a conversa inteira
  const editSegment = useCallback(
    async (n: number, patch: SegmentPatch) => {
      await queue.current;
      const updated = await api<Consultation>(`/consultations/${c.id}/segments/${n}`, { method: "PATCH", json: patch });
      setConsultation((prev) =>
        prev ? { ...prev, segmentos: updated.segmentos, insights: updated.insights ?? prev.insights, ia: updated.ia ?? prev.ia } : prev,
      );
      void translate([n]); // texto ou idioma novo: a tradução antiga foi descartada pelo servidor
    },
    [c.id, setConsultation, translate],
  );

  // pausa de privacidade, retomada e revogação do consentimento: viram marcos na transcrição
  async function marker(tipo: "pausa" | "retomada" | "revogado") {
    setError(null);
    try {
      await stopAll();
      await queue.current;
      const updated = await api<Consultation>(`/consultations/${c.id}/marker`, { method: "POST", json: { tipo } });
      setConsultation((prev) =>
        prev ? { ...prev, segmentos: updated.segmentos, consentimento: updated.consentimento, insights: updated.insights ?? prev.insights } : prev,
      );
      if (tipo === "revogado") unsent.current = []; // revogado: o que não chegou ao servidor não entra mais (o /finish recusaria o corpo)
      setConfirmRevoke(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao registrar o marco de privacidade");
    }
  }

  function submitTyped(e: React.FormEvent) {
    e.preventDefault();
    const texto = typed.trim();
    if (!texto) return;
    send([{ falante: speaker, texto, origem: "digitado" }]);
    setTyped("");
  }

  const ia = c.ia;
  const pending = c.segmentos.length - (ia?.falas ?? 0);
  const modes: { key: Mode; label: string; short: string; Icon: typeof Mic; show: boolean }[] = [
    { key: "microfone", label: "Microfone", short: "Microfone", Icon: Mic, show: browserStt },
    { key: "servidor", label: "Servidor (Whisper)", short: "Servidor", Icon: Server, show: config.stt_servidor },
    { key: "digitar", label: "Digitar", short: "Digitar", Icon: Keyboard, show: true },
    { key: "demo", label: "Simular consulta", short: "Simular", Icon: Sparkles, show: !!c.roteiro_demo },
  ];

  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-4">
          <div className="flex items-center gap-2">
            <span
              className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-sm font-bold", capturing ? "bg-critico-bg text-critico" : "bg-muted text-muted-foreground")}
            >
              <Radio aria-hidden className={cn("size-4", capturing && "animate-pulse")} />
              {capturing ? "Ao vivo" : "Pausado"}
            </span>
            <span className="font-mono text-lg tabular-nums" aria-label="Tempo de consulta">
              {clock(elapsed)}
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5 text-xs">
            <Badge variant="secondary">{KIND_LABEL[c.tipo]}</Badge>
            <Badge variant="outline">{c.especialidade_rotulo}</Badge>
            {c.idioma_paciente !== "pt-BR" && (
              <Badge variant="info">Tradução: {config.idiomas.find((i) => i.value === c.idioma_paciente)?.label}</Badge>
            )}
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground" role="status">
              {aiRunning ? (
                <span className="inline-flex items-center gap-1">
                  <Loader2 aria-hidden className="size-3.5 animate-spin" /> IA analisando a conversa…
                </span>
              ) : ia ? (
                <>
                  <Bot aria-hidden className="mr-0.5 inline size-3.5" />
                  {ia.modo === "real" ? `IA (${ia.modelo})` : "Regras (modo demonstração)"} até a fala {ia.falas}
                  {pending > 0 && ` · ${pending} nova(s)`}
                </>
              ) : (
                "Regras atualizam a cada fala"
              )}
            </span>
            {!surgical && c.insights?.regras_ms != null && (
              <Badge variant="outline" className="text-[11px]" data-testid="chip-latencia" title="Tempo das regras na última fala e fala até onde a IA analisou">
                regras {c.insights.regras_ms} ms{ia ? ` · IA até fala ${ia.falas}` : " · IA ainda não rodou"}
              </Badge>
            )}
            {!surgical && (
              <label className="inline-flex min-h-9 items-center gap-1.5 text-xs">
                <input type="checkbox" checked={autoAI} onChange={(e) => setAutoAI(e.target.checked)} className="size-4" /> IA automática
              </label>
            )}
            {c.idioma_paciente !== "pt-BR" && (
              <label className="inline-flex min-h-9 items-center gap-1.5 text-xs">
                <input type="checkbox" checked={autoVoice} onChange={(e) => setAutoVoice(e.target.checked)} className="size-4" /> Voz automática ao paciente
              </label>
            )}
            {consent && consentState === "ativo" && (
              <Button variant="outline" size="sm" onClick={() => void marker("pausa")}>
                <Pause aria-hidden /> Pausa de privacidade
              </Button>
            )}
            {consent && consentState === "pausado" && (
              <Button variant="outline" size="sm" onClick={() => void marker("retomada")}>
                <ShieldPlus aria-hidden /> Retomar transcrição
              </Button>
            )}
            {consent && consentState !== "revogado" &&
              (confirmRevoke ? (
                <Button variant="destructive" size="sm" onClick={() => void marker("revogado")}>
                  <ShieldOff aria-hidden /> Confirmar revogação
                </Button>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setConfirmRevoke(true)}>
                  <ShieldOff aria-hidden /> Revogar consentimento
                </Button>
              ))}
            <Button variant="outline" size="sm" onClick={() => void runAI(true)} disabled={aiRunning || c.segmentos.length === 0 || !canAI}>
              <RefreshCw aria-hidden /> Analisar agora
            </Button>
            <Button size="sm" onClick={() => void finish()} disabled={finishing || c.segmentos.length === 0}>
              {finishing ? <Loader2 aria-hidden className="animate-spin" /> : <FileCheck2 aria-hidden />}
              {surgical ? "Finalizar relatório" : "Finalizar e gerar nota"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {consent && (consent.recusou || !consent.permite.ia || consentState !== "ativo") && (
        <Alert variant={consentState === "ativo" ? "info" : "atencao"} role="status" data-testid="aviso-consentimento">
          {consentState === "revogado"
            ? "Consentimento revogado: a captura foi encerrada. Finalize para gerar a nota só com as regras."
            : consentState === "pausado"
              ? "Pausa de privacidade: nada está sendo transcrito. Retome quando o paciente autorizar."
              : consent.recusou
                ? "O paciente recusou a IA: as regras do HousIA analisam a conversa e a nota sai sem IA."
                : "IA externa não autorizada: só as regras do HousIA analisam a conversa."}
        </Alert>
      )}

      {aguardando > 0 && (
        <Alert variant="atencao" role="status" data-testid="falas-aguardando-rede">
          {aguardando} {aguardando === 1 ? "fala aguardando rede" : "falas aguardando rede"}
          <span className="text-xs"> — ficam guardadas só neste aparelho, cifradas, e são enviadas em ordem quando a rede voltar.</span>
          <button
            className="ml-2 underline"
            onClick={() => {
              filaParada.current = false;
              flushOffline();
            }}
          >
            tentar agora
          </button>
        </Alert>
      )}

      {error && (
        <Alert variant="critico" role="alert">
          {error}
          <button className="ml-2 underline" onClick={() => setError(null)}>
            fechar
          </button>
        </Alert>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <section aria-label="Captura e transcrição" className="space-y-3 lg:sticky lg:top-16 lg:self-start">
          <Card>
            <CardContent className="space-y-3 pt-4">
              <div role="tablist" aria-label="Como capturar a conversa" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
                {modes
                  .filter((m) => m.show)
                  .map(({ key, label, short, Icon }) => (
                    <button
                      key={key}
                      role="tab"
                      aria-selected={mode === key}
                      aria-label={label}
                      title={label}
                      onClick={() => {
                        void stopAll();
                        setMode(key);
                        if (key === "microfone" && mode !== "microfone") setSpeaker(startSpeaker(key));
                      }}
                      className={cn(
                        "inline-flex min-h-10 items-center gap-1.5 rounded-md px-3 text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        mode === key ? "bg-card text-primary shadow-sm" : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      <Icon aria-hidden className="size-4" /> {short}
                    </button>
                  ))}
              </div>

              {mode !== "demo" && (
                <fieldset>
                  <legend className="mb-1 text-xs font-bold text-muted-foreground">
                    Quem está falando ({surgical ? "Alt+1 a 6" : "Alt+1, 2, 3"} · Alt+0 automático)
                  </legend>
                  <div className="flex flex-wrap gap-1.5">
                    {speakers.map((s, i) => (
                      <button
                        key={s}
                        type="button"
                        aria-pressed={speaker === s}
                        aria-keyshortcuts={`Alt+${s === "desconhecido" ? 0 : i + 1}`}
                        onClick={() => setSpeaker(s)}
                        className={cn(
                          "min-h-10 rounded-md border px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          speaker === s ? SPEAKER_STYLE[s] : "bg-card text-muted-foreground hover:bg-secondary",
                        )}
                      >
                        {s === "desconhecido" ? "Automático" : speakerLabel(s, proLabel)}
                      </button>
                    ))}
                  </div>
                </fieldset>
              )}

              {mode === "microfone" && (
                <div className="space-y-2">
                  {!speechSupported() && (
                    <Alert variant="atencao">
                      Este navegador não tem reconhecimento de fala.{" "}
                      {config.stt_servidor ? "Use o modo Servidor (transcrição no servidor) ou Digitar." : "Use o modo Digitar, ou Chrome/Edge."}
                    </Alert>
                  )}
                  <Button
                    size="lg"
                    variant={mic.listening ? "destructive" : "default"}
                    className="w-full"
                    onClick={() => (mic.listening ? void mic.stop() : mic.start())}
                    disabled={!speechSupported() || captureBlocked}
                  >
                    {mic.listening ? <MicOff aria-hidden /> : <Mic aria-hidden />}
                    {mic.listening ? "Pausar transcrição" : "Iniciar transcrição ao vivo"}
                  </Button>
                  {mic.error && <p className="text-sm text-critico">{mic.error}</p>}
                  {speaker === "desconhecido" && (
                    <p className="text-xs text-muted-foreground">
                      Falante automático: o HousIA identifica quem falou (pergunta do profissional, resposta do paciente) e a IA
                      corrige depois. Fixe o falante nos botões acima se preferir.
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Reconhecimento de fala do navegador ({lang}). O áudio não é gravado pelo HousIA; em produção, use a transcrição no
                    servidor (on-premise) — ver ADR 0009.
                  </p>
                </div>
              )}

              {mode === "servidor" && (
                <div className="space-y-2">
                  <Button
                    size="lg"
                    variant={recorder.recording ? "destructive" : "default"}
                    className="w-full"
                    onClick={() => (recorder.recording ? void recorder.stop() : void recorder.start())}
                    disabled={captureBlocked || (consent ? !consent.permite.transcricao_servidor : false)}
                  >
                    {recorder.recording ? <MicOff aria-hidden /> : <Mic aria-hidden />}
                    {recorder.recording ? "Parar gravação" : "Gravar e transcrever no servidor"}
                  </Button>
                  {recorder.error && <p className="text-sm text-critico">{recorder.error}</p>}
                  <p className="text-xs text-muted-foreground">Trechos de 8 s vão para o serviço de transcrição configurado e são descartados.</p>
                </div>
              )}

              {(mode === "microfone" || mode === "servidor") && (
                <div className="space-y-2 rounded-md border p-2" data-testid="painel-microfone">
                  <label className="block text-xs font-bold text-muted-foreground" htmlFor="seletor-microfone">
                    Microfone
                  </label>
                  <div className="flex flex-wrap items-center gap-2">
                    <NativeSelect
                      id="seletor-microfone"
                      value={mics.deviceId}
                      onChange={(e) => mics.setDeviceId(e.target.value)}
                      className="min-w-0 flex-1"
                    >
                      <option value="">Padrão do aparelho</option>
                      {mics.devices.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.label}
                        </option>
                      ))}
                    </NativeSelect>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => (audioTest.testing ? audioTest.stop() : void audioTest.start())}
                      disabled={capturing}
                    >
                      {audioTest.testing ? "Parar teste" : "Testar áudio"}
                    </Button>
                  </div>
                  <div
                    role="meter"
                    aria-label="Nível do microfone"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(audioTest.level * 100)}
                    className="h-2 overflow-hidden rounded-full bg-muted"
                  >
                    <div className="h-full bg-primary transition-[width] duration-75" style={{ width: `${Math.round(audioTest.level * 100)}%` }} />
                  </div>
                  {audioTest.error && <p className="text-sm text-critico">{audioTest.error}</p>}
                  <p className="text-xs text-muted-foreground">
                    O teste é só local: nada é gravado nem enviado.{" "}
                    {mode === "microfone" ? "A transcrição ao vivo do navegador usa o microfone padrão do sistema; a escolha vale para o modo Servidor e o teste." : ""}
                    {wakeLock ? " A tela fica acesa durante a captura." : ""}
                  </p>
                </div>
              )}

              {mode === "digitar" && (
                <form onSubmit={submitTyped} className="flex gap-2">
                  <Input
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                    placeholder={`Fala de ${speaker === "desconhecido" ? "quem estiver falando" : speakerLabel(speaker, proLabel).toLowerCase()}…`}
                    aria-label="Fala"
                  />
                  <Button type="submit" disabled={!typed.trim() || captureBlocked}>
                    <Send aria-hidden /> Enviar
                  </Button>
                </form>
              )}

              {mode === "demo" && (
                <div className="space-y-2">
                  <p className="text-sm">
                    <span className="font-bold">Roteiro sintético:</span> {script.data?.titulo ?? "carregando…"}
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button onClick={demo.playing ? demo.pause : demo.play} disabled={!script.data || demo.done || captureBlocked}>
                      {demo.playing ? <Pause aria-hidden /> : <Play aria-hidden />}
                      {demo.done ? "Roteiro concluído" : demo.playing ? "Pausar" : demoStart > 0 ? "Continuar simulação" : "Simular consulta"}
                    </Button>
                    <label className="inline-flex items-center gap-1.5 text-sm">
                      Velocidade
                      <NativeSelect value={demo.speed} onChange={(e) => demo.setSpeed(Number(e.target.value))} className="h-10 w-24">
                        <option value={1}>1×</option>
                        <option value={2}>2×</option>
                        <option value={4}>4×</option>
                        <option value={8}>8×</option>
                      </NativeSelect>
                    </label>
                    {script.data && (
                      <span className="text-xs text-muted-foreground">
                        fala {Math.min(demo.index, script.data.falas.length)} de {script.data.falas.length}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Reproduz uma conversa fictícia como se fosse transcrita ao vivo — para demonstrar sem microfone.
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <Transcript
            segments={c.segmentos}
            interim={interim}
            proLabel={proLabel}
            patientLanguage={c.idioma_paciente}
            onEdit={editSegment}
            speakers={speakers.filter((sp) => sp !== "desconhecido")}
          />
          <PrivacyPanel panel={c.privacidade ?? config.privacidade} expurgadaEm={c.privacidade?.expurgada_em} />
        </section>

        <section aria-label="Assistente" className="space-y-3">
          {!surgical && (
            <CopilotPanel
              consultId={c.id}
              patientId={c.patient_id}
              insights={c.insights}
              pushed={pushed}
              ptt={{ active: ptt.active, interim: ptt.interim, error: ptt.error, supported: browserStt && speechSupported(), start: ptt.start, stop: ptt.stop }}
            />
          )}
          {c.insights?.relatorio_cirurgico ? (
            <SurgicalPanel
              report={c.insights.relatorio_cirurgico}
              patientId={patient.id}
              onChecklist={markChecklist}
              consultation={c}
              onInsights={refreshInsights}
            />
          ) : c.insights ? (
            <SmartAppsPanel
              insights={c.insights}
              onMainComplaint={setMainComplaint}
              onHypothesisDecision={decideHypothesis}
              onInsightAction={decideInsight}
              orders={{
                consultId: c.id,
                onInsights: (insights) => setConsultation((prev) => (prev ? { ...prev, insights: newerInsights(prev.insights, insights) } : prev)),
              }}
            />
          ) : null}
        </section>
      </div>
    </div>
  );
}
