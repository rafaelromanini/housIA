"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { DemoScript, Speaker } from "@/lib/api";

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
        setListening(false);
      }
    };
    rec.current = r;
    wanted.current = true;
    setError(null);
    r.start();
    setListening(true);
  }, []);

  const stop = useCallback(() => {
    wanted.current = false;
    rec.current?.stop();
    setListening(false);
    setInterim("");
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

/* ---------- Gravação para transcrição no servidor (Whisper on-premise etc.) ---------- */
export function useServerRecorder(onChunk: (audio: Blob) => void, seconds = 8) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wanted = useRef(false);
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

  const stop = useCallback(() => {
    wanted.current = false;
    if (timer.current) clearTimeout(timer.current);
    if (recorder.current?.state === "recording") recorder.current.stop();
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    setRecording(false);
  }, []);

  useEffect(() => () => stop(), [stop]);
  return { recording, error, start, stop };
}

/* ---------- Simulação com roteiro sintético (demo sem microfone) ---------- */
export function useDemoPlayer(
  script: DemoScript | null,
  startAt: number,
  onLine: (line: { falante: Speaker; texto: string; t: number }) => void,
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
