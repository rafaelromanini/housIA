"use client";

import { useEffect, useRef, useState } from "react";
import { Languages, Volume2 } from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { Button } from "@/components/ui/button";
import type { Segment, Speaker } from "@/lib/api";
import { cn } from "@/lib/utils";

export const SPEAKER_STYLE: Record<Speaker, string> = {
  medico: "bg-primary text-primary-foreground",
  paciente: "bg-accent text-accent-foreground",
  acompanhante: "bg-info-bg text-info border border-info-border",
  anestesista: "bg-ok-bg text-ok border border-ok-border",
  circulante: "bg-atencao-bg text-atencao border border-atencao-border",
  instrumentador: "bg-secondary text-secondary-foreground border border-border",
  desconhecido: "bg-muted text-muted-foreground",
};

/** Equipe de sala (modo cirúrgico): botões e atalhos Alt+4, Alt+5 e Alt+6. */
export const TEAM_SPEAKERS: Speaker[] = ["anestesista", "circulante", "instrumentador"];

export function speakerLabel(s: Speaker, proLabel: string) {
  return {
    medico: proLabel,
    paciente: "Paciente",
    acompanhante: "Acompanhante",
    anestesista: "Anestesista",
    circulante: "Circulante",
    instrumentador: "Instrumentador(a)",
    desconhecido: "Quem fala?",
  }[s];
}

export const clock = (t: number) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

const VOICE_LANG: Record<string, string> = { "pt-BR": "pt-BR", es: "es-ES", en: "en-US", fr: "fr-FR", it: "it-IT", de: "de-DE", zh: "zh-CN", ar: "ar-SA", ht: "fr-HT" };

/** Transcrição ao vivo: falas com quem falou, tempo, tradução e destaque quando um Smart App cita a fala. */
export function Transcript({
  segments,
  interim,
  proLabel,
  patientLanguage,
}: {
  segments: Segment[];
  interim: { falante: Speaker; texto: string } | null;
  proLabel: string;
  patientLanguage: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const { patient } = usePatient();

  // acompanha a conversa enquanto a pessoa está no fim da lista
  useEffect(() => {
    const el = box.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
  }, [segments.length, interim?.texto]);

  // "fala N" nos Smart Apps leva até o trecho citado
  useEffect(() => {
    const onFala = (e: Event) => {
      const n = (e as CustomEvent<number>).detail;
      setFocus(n);
      document.getElementById(`fala-${n}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
      window.setTimeout(() => setFocus((f) => (f === n ? null : f)), 2600);
    };
    window.addEventListener("housia:fala", onFala);
    return () => window.removeEventListener("housia:fala", onFala);
  }, []);

  function speak(text: string) {
    const u = new SpeechSynthesisUtterance(reidentify(text, patient.nome));
    u.lang = VOICE_LANG[patientLanguage] ?? patientLanguage;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  return (
    <div
      ref={box}
      className="max-h-[62vh] min-h-64 overflow-y-auto rounded-md border bg-background p-3"
      aria-live="polite"
      aria-label="Transcrição da conversa"
    >
      {segments.length === 0 && !interim && (
        <p className="py-10 text-center text-sm text-muted-foreground">
          A transcrição aparece aqui em tempo real. Inicie o microfone, digite as falas ou simule a consulta de demonstração.
        </p>
      )}
      <ol className="space-y-2">
        {segments.map((s) => (
          <li
            key={s.n}
            id={`fala-${s.n}`}
            className={cn(
              "rounded-md p-2 transition-colors",
              s.falante === "medico" || TEAM_SPEAKERS.includes(s.falante) ? "bg-card" : "bg-secondary/50",
              focus === s.n && "ring-2 ring-ring bg-atencao-bg",
            )}
          >
            <div className="mb-0.5 flex flex-wrap items-center gap-2 text-xs">
              <span className={cn("rounded px-1.5 py-0.5 font-bold", SPEAKER_STYLE[s.falante])}>
                {speakerLabel(s.falante, proLabel)}
              </span>
              <span className="tabular-nums text-muted-foreground">
                {clock(s.t)} · fala {s.n}
              </span>
              {s.falante_auto && <span className="text-muted-foreground">(quem falou: detectado automaticamente)</span>}
            </div>
            <p className="leading-snug">{s.texto}</p>
            {s.traducao && (
              <p className="mt-1 flex items-start gap-1.5 text-sm text-muted-foreground">
                <Languages aria-hidden className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">{reidentify(s.traducao, patient.nome)}</span>
                {s.falante === "medico" && (
                  <Button variant="ghost" size="sm" className="min-h-8 px-2" onClick={() => speak(s.traducao!)} aria-label="Ouvir a tradução">
                    <Volume2 aria-hidden />
                  </Button>
                )}
              </p>
            )}
          </li>
        ))}
        {interim && (
          <li className="rounded-md border border-dashed p-2" aria-live="off">
            <div className="mb-0.5 flex items-center gap-2 text-xs">
              <span className={cn("rounded px-1.5 py-0.5 font-bold", SPEAKER_STYLE[interim.falante])}>
                {speakerLabel(interim.falante, proLabel)}
              </span>
              <span className="inline-flex items-center gap-1 text-muted-foreground">
                <span className="size-2 animate-pulse rounded-full bg-critico" aria-hidden /> transcrevendo…
              </span>
            </div>
            <p className="italic text-muted-foreground">{interim.texto}</p>
          </li>
        )}
      </ol>
    </div>
  );
}
