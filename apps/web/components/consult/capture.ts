"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import type { ConsultInsights, DemoScript, Segment, Speaker } from "@/lib/api";

/* ---------- Reconhecimento de fala do navegador (Web Speech API) ---------- */
interface RecognitionResult {
  isFinal: boolean;
  0: { transcript: string };
}
interface RecognitionEvent {
  resultIndex: number;
  results: ArrayLike<RecognitionResult>;
}
interface Recognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function speechSupported() {
  return recognitionCtor() !== null;
}

/** Transcrição ao vivo pelo navegador: entrega trechos finais e mostra o texto parcial enquanto a pessoa fala. */
export function useSpeechCapture(lang: string, onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const wanted = useRef(false);
  const running = useRef(false);
  const flushing = useRef<(() => void)[]>([]);
  const langRef = useRef(lang);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) {
      setError("Este navegador não tem reconhecimento de fala. Use Chrome ou Edge, ou o modo Digitar.");
      return;
    }
    const r = new Ctor();
    r.lang = langRef.current;
    r.continuous = true;
    r.interimResults = true;
    r.onresult = (e) => {
      let partial = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) {
          const text = res[0].transcript.trim();
          if (text) onFinalRef.current(text);
        } else {
          partial += res[0].transcript;
        }
      }
      setInterim(partial);
    };
    r.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        wanted.current = false;
        setError("Permissão do microfone negada. Libere o microfone no navegador.");
      } else if (e.error !== "no-speech" && e.error !== "aborted") {
        setError(`Reconhecimento de fala: ${e.error}`);
      }
    };
    // o Chrome encerra após silêncio: reinicia enquanto a captura estiver ligada
    r.onend = () => {
      setInterim("");
      if (wanted.current) {
        r.lang = langRef.current;
        try {
          r.start();
        } catch {
          /* já reiniciado */
        }
      } else {
        running.current = false;
        setListening(false);
        // o 'end' vem depois do último resultado final: quem esperava o flush já pode seguir
        flushing.current.splice(0).forEach((done) => done());
      }
    };
    rec.current = r;
    wanted.current = true;
    setError(null);
    r.start();
    running.current = true;
    setListening(true);
  }, []);

  /** Para a captura e devolve uma Promise resolvida quando o reconhecimento entrega o último resultado ('end'),
   * para que a fala em andamento não se perca ao finalizar. Tem um limite de 2,5 s para nunca travar a tela. */
  const stop = useCallback((): Promise<void> => {
    wanted.current = false;
    setListening(false);
    setInterim("");
    if (!rec.current || !running.current) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const timer = window.setTimeout(resolve, 2500);
      flushing.current.push(() => {
        window.clearTimeout(timer);
        resolve();
      });
      rec.current?.stop();
    });
  }, []);

  // trocar o idioma (ex.: paciente fala espanhol) reinicia o reconhecimento no novo idioma
  useEffect(() => {
    langRef.current = lang;
    if (wanted.current) rec.current?.stop();
  }, [lang]);

  useEffect(
    () => () => {
      wanted.current = false;
      rec.current?.abort();
    },
    [],
  );

  return { listening, interim, error, start, stop };
}

/** Push-to-talk do copiloto (Alt+Q): reconhecimento próprio, separado da transcrição da consulta. O que é dito aqui
 * vira pergunta ao copiloto e nunca uma fala (Segment) da conversa. */
export function usePushToTalk(onText: (text: string) => void, onBegin?: () => void, onFinish?: () => void) {
  const [active, setActive] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const finals = useRef("");
  const onTextRef = useRef(onText);
  const beginRef = useRef(onBegin);
  const finishRef = useRef(onFinish);
  onTextRef.current = onText;
  beginRef.current = onBegin;
  finishRef.current = onFinish;

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) {
      setError("Este navegador não tem reconhecimento de fala. Digite a pergunta.");
      return;
    }
    if (rec.current) return;
    const r = new Ctor();
    r.lang = "pt-BR";
    r.continuous = false;
    r.interimResults = true;
    finals.current = "";
    r.onresult = (e) => {
      let partial = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finals.current += `${res[0].transcript} `;
        else partial += res[0].transcript;
      }
      setInterim(partial);
    };
    r.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") setError("Permissão do microfone negada.");
    };
    r.onend = () => {
      rec.current = null;
      setActive(false);
      setInterim("");
      const text = `${finals.current}`.trim();
      finals.current = "";
      finishRef.current?.();
      if (text) onTextRef.current(text);
    };
    rec.current = r;
    setError(null);
    beginRef.current?.();
    setActive(true);
    try {
      r.start();
    } catch {
      rec.current = null;
      setActive(false);
      finishRef.current?.();
    }
  }, []);

  const stop = useCallback(() => rec.current?.stop(), []);
  useEffect(() => () => rec.current?.abort(), []);
  return { active, interim, error, start, stop };
}

/* ---------- Gravação para transcrição no servidor (Whisper on-premise etc.) ---------- */
export function useServerRecorder(onChunk: (audio: Blob) => void, seconds = 8) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wanted = useRef(false);
  const stopped = useRef<(() => void)[]>([]);
  const onChunkRef = useRef(onChunk);
  onChunkRef.current = onChunk;

  const cycle = useCallback(() => {
    if (!stream.current) return;
    const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    const r = new MediaRecorder(stream.current, mime ? { mimeType: mime } : undefined);
    const chunks: Blob[] = [];
    r.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    // cada trecho é um arquivo completo: dá para transcrever sem juntar com o anterior
    r.onstop = () => {
      const blob = new Blob(chunks, { type: r.mimeType || "audio/webm" });
      if (blob.size > 2000) onChunkRef.current(blob);
      if (wanted.current) cycle();
      else stopped.current.splice(0).forEach((done) => done()); // último trecho entregue à fila de envio
    };
    recorder.current = r;
    r.start();
    timer.current = setTimeout(() => r.state === "recording" && r.stop(), seconds * 1000);
  }, [seconds]);

  const start = useCallback(async () => {
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError("Não foi possível acessar o microfone.");
      return;
    }
    wanted.current = true;
    setError(null);
    setRecording(true);
    cycle();
  }, [cycle]);

  /** Para a gravação; a Promise resolve quando o último trecho foi entregue para envio (o upload segue na fila). */
  const stop = useCallback((): Promise<void> => {
    wanted.current = false;
    if (timer.current) clearTimeout(timer.current);
    const active = recorder.current?.state === "recording";
    const done = active
      ? new Promise<void>((resolve) => {
          const t = window.setTimeout(resolve, 2500);
          stopped.current.push(() => {
            window.clearTimeout(t);
            resolve();
          });
        })
      : Promise.resolve();
    if (active) recorder.current?.stop();
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    setRecording(false);
    return done;
  }, []);

  useEffect(
    () => () => {
      void stop();
    },
    [stop],
  );
  return { recording, error, start, stop };
}

/* ---------- Transcrição em fluxo no servidor (#11): AudioWorklet → PCM 16 kHz → WebSocket ---------- */
// O worklet reamostra para 16 kHz mono e entrega blocos de 500 ms (int16). O áudio nunca é gravado no navegador.
const PCM_WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.pos = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const ratio = sampleRate / 16000;
    for (; this.pos < ch.length; this.pos += ratio) this.buf.push(ch[Math.floor(this.pos)]);
    this.pos -= ch.length;
    while (this.buf.length >= 8000) {
      const out = new Int16Array(8000);
      const part = this.buf.splice(0, 8000);
      for (let i = 0; i < 8000; i++) out[i] = Math.max(-1, Math.min(1, part[i])) * 32767;
      this.port.postMessage(out.buffer, [out.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);
`;

export interface StreamFinal {
  novos: Segment[];
  insights: ConsultInsights | null;
}

export function useServerStream(
  consultationId: number,
  speaker: Speaker,
  onFinal: (res: StreamFinal) => void,
) {
  const [streaming, setStreaming] = useState(false);
  const [partial, setPartial] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const media = useRef<MediaStream | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const closing = useRef<(() => void)[]>([]);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const release = useCallback(() => {
    media.current?.getTracks().forEach((t) => t.stop());
    media.current = null;
    void ctx.current?.close();
    ctx.current = null;
    setStreaming(false);
    setPartial(null);
  }, []);

  const start = useCallback(async () => {
    setError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } });
    } catch {
      setError("Não foi possível acessar o microfone.");
      return;
    }
    media.current = stream;
    const socket = new WebSocket(`${API_URL.replace(/^http/, "ws")}/ws/consultations/${consultationId}/audio?falante=${speaker}`);
    socket.binaryType = "arraybuffer";
    ws.current = socket;
    socket.onmessage = (e) => {
      const m = JSON.parse(String(e.data));
      if (m.tipo === "parcial") setPartial(m.texto);
      else if (m.tipo === "final") {
        setPartial(null);
        onFinalRef.current({ novos: m.novos ?? [], insights: m.insights ?? null });
      } else if (m.tipo === "erro") {
        setError(m.mensagem);
        release();
      } else if (m.tipo === "fim") closing.current.splice(0).forEach((done) => done());
    };
    socket.onclose = () => {
      closing.current.splice(0).forEach((done) => done());
      release();
    };
    socket.onerror = () => setError("A conexão com a transcrição no servidor falhou.");
    try {
      const audio = new AudioContext();
      ctx.current = audio;
      const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: "text/javascript" }));
      await audio.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      const node = new AudioWorkletNode(audio, "pcm16");
      node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(e.data);
      };
      audio.createMediaStreamSource(stream).connect(node);
      setStreaming(true);
    } catch {
      setError("Este navegador não consegue capturar áudio para o servidor. Use o modo Microfone ou Digitar.");
      socket.close();
      release();
    }
  }, [consultationId, speaker, release]);

  /** Para a captura; a Promise resolve quando o servidor fechou a última frase (ou após 4 s). */
  const stop = useCallback((): Promise<void> => {
    media.current?.getTracks().forEach((t) => t.stop());
    const socket = ws.current;
    ws.current = null;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      release();
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      const t = window.setTimeout(resolve, 4000);
      closing.current.push(() => {
        window.clearTimeout(t);
        resolve();
      });
      socket.send(JSON.stringify({ tipo: "fim" }));
    }).finally(release);
  }, [release]);

  // troca de falante no meio da transmissão: o servidor fecha a frase de quem falava e muda o idioma
  useEffect(() => {
    const socket = ws.current;
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ tipo: "falante", falante: speaker }));
  }, [speaker]);

  useEffect(
    () => () => {
      void stop();
    },
    [stop],
  );
  return { streaming, partial, error, start, stop };
}

/* ---------- Simulação com roteiro sintético (demo sem microfone) ---------- */
export function useDemoPlayer(
  script: DemoScript | null,
  startAt: number,
  onLine: (line: { falante: Speaker; texto: string; t: number; idioma?: string }) => void,
) {
  const [playing, setPlaying] = useState(false);
  const [index, setIndex] = useState(startAt);
  const [interim, setInterim] = useState<{ falante: Speaker; texto: string } | null>(null);
  const [speed, setSpeed] = useState(2);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const onLineRef = useRef(onLine);
  onLineRef.current = onLine;

  useEffect(() => setIndex(startAt), [startAt]);

  const clear = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  useEffect(() => {
    if (!playing || !script) return;
    if (index >= script.falas.length) {
      setPlaying(false);
      return;
    }
    const line = script.falas[index];
    const prev = index > 0 ? script.falas[index - 1].t : line.t;
    const total = Math.max(500, ((line.t - prev) * 1000) / speed);
    // mostra a fala "sendo transcrita" palavra a palavra, como no microfone
    const words = line.texto.split(" ");
    const typing = Math.min(total * 0.85, words.length * (220 / speed));
    words.forEach((_, i) => {
      timers.current.push(
        setTimeout(
          () => setInterim({ falante: line.falante, texto: words.slice(0, i + 1).join(" ") }),
          total - typing + (typing * i) / words.length,
        ),
      );
    });
    timers.current.push(
      setTimeout(() => {
        setInterim(null);
        onLineRef.current(line);
        setIndex((n) => n + 1);
      }, total),
    );
    return clear;
  }, [playing, index, script, speed]);

  return {
    playing,
    index,
    interim,
    speed,
    setSpeed,
    play: () => setPlaying(true),
    pause: () => {
      clear();
      setInterim(null);
      setPlaying(false);
    },
    done: !!script && index >= script.falas.length,
  };
}
