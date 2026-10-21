"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Languages, Pause, Pencil, Play, ShieldOff, Volume2 } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";

import { reidentify } from "@/components/ai/ai-badge";
import { usePatient } from "@/components/patient/patient-context";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import type { ConsultConfig, Segment, Speaker } from "@/lib/api";
import { useApi } from "@/lib/use-api";
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

// voz do navegador por idioma; fora dessa lista vale o próprio código BCP-47 (a lista completa vem de /consult/config)
export const VOICE_LANG: Record<string, string> = { "pt-BR": "pt-BR", es: "es-ES", en: "en-US", fr: "fr-FR", it: "it-IT", de: "de-DE", zh: "zh-CN", ar: "ar-SA", ht: "fr-HT" };

/** Transcrição ao vivo: falas com quem falou, tempo, tradução e destaque quando um Smart App cita a fala. */
const MARKER_META = {
  pausa: { label: "Pausa de privacidade", Icon: Pause },
  retomada: { label: "Transcrição retomada", Icon: Play },
  revogado: { label: "Consentimento revogado", Icon: ShieldOff },
} as const;

export type SegmentPatch = { texto?: string; falante?: Speaker; idioma?: string };

/** Transcrição: falas com quem falou, tempo, tradução, marcos de privacidade e, se `onEdit` vier, correção da fala. */
export function Transcript({
  segments,
  interim,
  proLabel,
  patientLanguage,
  onEdit,
  onConfirm,
  speakers,
  maxHeight = "max-h-[62vh]",
}: {
  segments: Segment[];
  interim: { falante: Speaker; texto: string } | null;
  proLabel: string;
  patientLanguage: string;
  onEdit?: (n: number, patch: SegmentPatch) => Promise<void>;
  onConfirm?: (n: number) => Promise<void>;
  speakers?: Speaker[];
  maxHeight?: string;
}) {
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [draftSpeaker, setDraftSpeaker] = useState<Speaker>("medico");
  const [draftLang, setDraftLang] = useState("pt-BR");
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const speakerOptions = speakers ?? (["medico", "paciente", "acompanhante"] as Speaker[]);

  async function save(s: Segment) {
    const patch: SegmentPatch = {};
    if (draft.trim() && draft.trim() !== s.texto) patch.texto = draft.trim();
    if (draftSpeaker !== s.falante) patch.falante = draftSpeaker;
    if (draftLang !== s.idioma) patch.idioma = draftLang;
    if (Object.keys(patch).length === 0) {
      setEditing(null);
      return;
    }
    setSaving(true);
    setEditError(null);
    try {
      await onEdit?.(s.n, patch);
      setEditing(null);
    } catch (e) {
      setEditError(e instanceof Error ? e.message : "Erro ao salvar a correção");
    } finally {
      setSaving(false);
    }
  }

  const box = useRef<HTMLDivElement>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [onlyClinical, setOnlyClinical] = useState(false);
  const hidden = segments.filter((s) => s.origem !== "marco" && (s.relevancia === "social" || s.relevancia === "administrativa")).length;
  const shown = onlyClinical ? segments.filter((s) => s.origem === "marco" || (s.relevancia !== "social" && s.relevancia !== "administrativa")) : segments;
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
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Checkbox id="so-clinico" checked={onlyClinical} onCheckedChange={(v) => setOnlyClinical(v === true)} />
        <Label htmlFor="so-clinico">Só clínico</Label>
        <span className="text-xs text-muted-foreground" aria-live="polite">
          {onlyClinical
            ? `${hidden} fala(s) social(is) ou administrativa(s) escondida(s)`
            : hidden > 0
              ? `${hidden} fala(s) fora do foco clínico`
              : ""}
        </span>
      </div>
    <div
      ref={box}
      className={cn(maxHeight, "min-h-64 overflow-y-auto rounded-md border bg-background p-3")}
      aria-live="polite"
      aria-label="Transcrição da conversa"
    >
      {segments.length === 0 && !interim && (
        <p className="py-10 text-center text-sm text-muted-foreground">
          A transcrição aparece aqui em tempo real. Inicie o microfone, digite as falas ou simule a consulta de demonstração.
        </p>
      )}
      <ol className="space-y-2">
        {shown.map((s) =>
          s.origem === "marco" && s.marco ? (
            <li
              key={s.n}
              id={`fala-${s.n}`}
              data-marco={s.marco}
              className="flex items-center gap-2 rounded-md border border-dashed border-atencao-border bg-atencao-bg px-2 py-1.5 text-sm text-atencao"
            >
              {(() => {
                const { Icon, label } = MARKER_META[s.marco];
                return (
                  <>
                    <Icon aria-hidden className="size-4" />
                    <span className="font-bold">{label}</span>
                    <span className="tabular-nums text-xs">{clock(s.t)}</span>
                  </>
                );
              })()}
            </li>
          ) : (
          <li
            key={s.n}
            id={`fala-${s.n}`}
            className={cn(
              "rounded-md p-2 transition-colors",
              s.falante === "medico" || TEAM_SPEAKERS.includes(s.falante) ? "bg-card" : "bg-secondary/50",
              focus === s.n && "ring-2 ring-ring bg-atencao-bg",
              (s.relevancia === "social" || s.relevancia === "administrativa") && "opacity-70",
            )}
            data-relevancia={s.relevancia ?? "clinica"}
          >
            <div className="mb-0.5 flex flex-wrap items-center gap-2 text-xs">
              <span className={cn("rounded px-1.5 py-0.5 font-bold", SPEAKER_STYLE[s.falante])}>
                {speakerLabel(s.falante, proLabel)}
              </span>
              <span className="tabular-nums text-muted-foreground">
                {clock(s.t)} · fala {s.n}
              </span>
              {(s.idioma !== "pt-BR" || patientLanguage !== "pt-BR") && s.origem !== "marco" && (
                <span className="rounded border px-1.5 uppercase text-muted-foreground" title="Idioma em que a fala foi dita" data-testid={`idioma-fala-${s.n}`}>
                  {s.idioma}
                </span>
              )}
              {s.editado_em && (
                <span
                  className="rounded border border-info-border bg-info-bg px-1.5 text-info"
                  title={`Corrigida por ${s.editado_por ?? "profissional"}${s.texto_original ? ` — original: “${s.texto_original}”` : ""}`}
                >
                  editada
                </span>
              )}
              {onEdit && editing !== s.n && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto min-h-8 px-2"
                  aria-label={`Editar a fala ${s.n}`}
                  onClick={() => {
                    setEditing(s.n);
                    setDraft(s.texto);
                    setDraftSpeaker(s.falante);
                    setDraftLang(s.idioma);
                    setEditError(null);
                  }}
                >
                  <Pencil aria-hidden />
                </Button>
              )}
              {s.falante_auto && <span className="text-muted-foreground">(quem falou: detectado automaticamente)</span>}
              {(s.relevancia === "social" || s.relevancia === "administrativa") && (
                <span className="rounded border px-1.5 text-muted-foreground">
                  {s.relevancia === "social" ? "social" : "administrativa"}
                  {s.relevancia_origem === "ia" ? " (IA)" : ""}
                </span>
              )}
            </div>
            {editing === s.n ? (
              <div className="space-y-2" data-testid={`editar-fala-${s.n}`}>
                <Label htmlFor={`texto-fala-${s.n}`}>Texto da fala {s.n}</Label>
                <Textarea id={`texto-fala-${s.n}`} value={draft} onChange={(e) => setDraft(e.target.value)} rows={3} maxLength={2000} />
                <div className="flex flex-wrap items-center gap-2">
                  <Label htmlFor={`falante-fala-${s.n}`}>Quem falou</Label>
                  <NativeSelect
                    id={`falante-fala-${s.n}`}
                    value={draftSpeaker}
                    onChange={(e) => setDraftSpeaker(e.target.value as Speaker)}
                    className="h-10 w-48"
                  >
                    {(speakerOptions.includes(s.falante) ? speakerOptions : [...speakerOptions, s.falante]).map((sp) => (
                      <option key={sp} value={sp}>
                        {speakerLabel(sp, proLabel)}
                      </option>
                    ))}
                  </NativeSelect>
                  <LanguageField id={`idioma-fala-edit-${s.n}`} value={draftLang} onChange={setDraftLang} />
                  <Button size="sm" onClick={() => void save(s)} disabled={saving || !draft.trim()}>
                    Salvar correção
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditing(null)} disabled={saving}>
                    Cancelar
                  </Button>
                </div>
                {editError && <p className="text-sm text-critico">{editError}</p>}
              </div>
            ) : (
              <p className="leading-snug">
                <TextoMarcado segmento={s} />
              </p>
            )}
            {s.corrigidos && s.corrigidos.length > 0 && (
              <p className="mt-0.5 text-xs text-muted-foreground" data-testid={`corrigido-${s.n}`}>
                <span className="rounded border border-info-border bg-info-bg px-1.5 text-info">corrigido</span>{" "}
                {s.corrigidos.map((c) => `“${c.de}” → ${c.para}`).join("; ")}
              </p>
            )}
            {s.confirmar && s.confirmar.length > 0 && !s.confirmado_em && (
              <p
                className="mt-1 flex flex-wrap items-center gap-2 rounded border border-atencao-border bg-atencao-bg p-1.5 text-sm text-atencao"
                role="alert"
                data-testid={`confirmar-ouvido-${s.n}`}
              >
                <AlertTriangle aria-hidden className="size-4 shrink-0" />
                <span>Confirme o que foi ouvido: {s.confirmar.map((w) => `“${w}”`).join(", ")} (baixa confiança). O alerta crítico fica em atenção até a confirmação.</span>
                {onConfirm && (
                  <Button size="sm" variant="outline" className="min-h-8" onClick={() => void onConfirm(s.n)} aria-label={`Confirmar o que foi ouvido na fala ${s.n}`}>
                    Confirmar
                  </Button>
                )}
              </p>
            )}
            {s.traducao_divergente && s.traducao_divergente.length > 0 && (
              <p className="mt-1 flex items-start gap-1.5 rounded border border-atencao-border bg-atencao-bg p-1.5 text-sm text-atencao" role="alert" data-testid={`traducao-divergente-${s.n}`}>
                <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
                <span>Tradução divergente: {s.traducao_divergente.join("; ")}. Confira a fala original.</span>
              </p>
            )}
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
          ),
        )}
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
    </div>
  );
}

/** Idioma de uma fala (chip editável): lista completa vinda da configuração (≥ 95 idiomas). */
function LanguageField({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const config = useApi<ConsultConfig>("/consult/config");
  return (
    <>
      <Label htmlFor={id}>Idioma da fala</Label>
      <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-10 w-48">
        {(config.data?.idiomas ?? [{ value, label: value }]).map((i) => (
          <option key={i.value} value={i.value}>
            {i.label}
          </option>
        ))}
      </NativeSelect>
    </>
  );
}

/** Texto da fala com as palavras de baixa confiança sublinhadas e os termos corrigidos destacados (#38). */
function TextoMarcado({ segmento: s }: { segmento: Segment }) {
  const baixas = new Set((s.palavras_baixas ?? []).map((w) => w.toLowerCase()));
  const corrigidos = new Set((s.corrigidos ?? []).map((c) => c.para.toLowerCase()));
  if (baixas.size === 0 && corrigidos.size === 0) return <>{s.texto}</>;
  const partes = s.texto.split(/(\s+)/);
  return (
    <>
      {partes.map((parte, i) => {
        const limpa = parte.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").toLowerCase();
        if (limpa && baixas.has(limpa)) {
          return (
            <span key={i} className="underline decoration-atencao decoration-wavy underline-offset-4" title={`Baixa confiança${s.confianca != null ? ` (trecho ${Math.round(s.confianca * 100)}%)` : ""}`}>
              {parte}
            </span>
          );
        }
        if (limpa && corrigidos.has(limpa)) {
          return (
            <span key={i} className="rounded bg-info-bg px-0.5 text-info" title="Corrigido por similaridade com o catálogo de medicamentos">
              {parte}
            </span>
          );
        }
        return parte;
      })}
    </>
  );
}
