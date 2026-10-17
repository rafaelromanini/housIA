"use client";

import { useEffect, useState } from "react";
import { Languages, Volume2 } from "lucide-react";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { api, type ConsultConfig, type Consultation } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useApi } from "@/lib/use-api";

type Summary = { id: number; status: Consultation["status"] };
const MOCK_MARK = "tradução com LLM real";

/**
 * Legenda grande no idioma do paciente (rota /pacientes/[id]/consulta/legenda): abre numa segunda tela ou no tablet
 * voltado ao paciente e mostra as últimas falas do profissional já traduzidas, atualizando a cada 2 segundos.
 */
export function CaptionScreen() {
  const { patient } = usePatient();
  const { user } = useAuth();
  const allowed = user?.role === "medico" || user?.role === "enfermagem";
  const config = useApi<ConsultConfig>(allowed ? "/consult/config" : null);
  const [consultation, setConsultation] = useState<Consultation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) return;
    let alive = true;
    let id: number | null = null;
    async function tick() {
      try {
        if (id === null) {
          const list = await api<Summary[]>(`/patients/${patient.id}/consultations`);
          id = list.find((c) => c.status === "em_andamento")?.id ?? null;
        }
        if (id !== null) {
          const c = await api<Consultation>(`/consultations/${id}`);
          if (alive) setConsultation(c);
          if (c.status !== "em_andamento") id = null;
        } else if (alive) {
          setConsultation(null);
        }
        if (alive) setError(null);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Erro ao carregar a legenda");
      }
    }
    void tick();
    const timer = window.setInterval(() => void tick(), 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [allowed, patient.id]);

  if (!allowed) return <Alert variant="atencao">A legenda é exibida para médicos e enfermagem.</Alert>;

  const lang = consultation?.idioma_paciente ?? "pt-BR";
  const speechLang = config.data?.idiomas.find((i) => i.value === lang)?.web_speech ?? lang;
  const label = config.data?.idiomas.find((i) => i.value === lang)?.label ?? lang;
  // falas do profissional com tradução real (o aviso do modo demonstração não é legenda)
  const lines = (consultation?.segmentos ?? [])
    .filter((s) => s.falante === "medico" && s.traducao && !s.traducao.includes(MOCK_MARK))
    .slice(-3);
  const last = lines[lines.length - 1];

  function speak(text: string) {
    const u = new SpeechSynthesisUtterance(reidentify(text, patient.nome));
    u.lang = speechLang;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  return (
    <section aria-labelledby="legenda-titulo" className="space-y-4" data-testid="legenda">
      <header className="flex flex-wrap items-center gap-3">
        <Languages aria-hidden className="size-6 text-primary" />
        <h2 id="legenda-titulo" className="text-xl font-bold">
          Legenda da consulta {consultation ? `— ${label}` : ""}
        </h2>
        {last && (
          <Button variant="outline" size="sm" onClick={() => speak(last.traducao!)} aria-label="Ouvir a última fala">
            <Volume2 aria-hidden /> Ouvir
          </Button>
        )}
      </header>
      {error && <Alert variant="atencao">{error}</Alert>}
      {!consultation && !error && (
        <p className="text-lg text-muted-foreground">Nenhuma consulta em andamento para este paciente.</p>
      )}
      {consultation && lines.length === 0 && (
        <p className="text-lg text-muted-foreground">Aguardando a fala do profissional…</p>
      )}
      <div lang={speechLang} dir="auto" aria-live="polite" className="space-y-4" data-testid="legenda-falas">
        {lines.map((s, i) => (
          <p
            key={s.n}
            className={i === lines.length - 1 ? "text-4xl font-bold leading-tight md:text-5xl" : "text-2xl leading-snug text-muted-foreground"}
          >
            {reidentify(s.traducao!, patient.nome)}
          </p>
        ))}
      </div>
    </section>
  );
}
