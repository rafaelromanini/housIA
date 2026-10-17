"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ClipboardList, FilePlus2, ListPlus, Loader2, MessageCircleQuestion, Mic, Send } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type ConsultInsights, type CopilotInsertion, type CopilotMessage, type CopilotState } from "@/lib/api";
import { cn } from "@/lib/utils";

const TARGETS: { value: string; label: string }[] = [
  { value: "avaliacao", label: "Avaliação" },
  { value: "plano", label: "Plano" },
  { value: "hda", label: "HDA" },
  { value: "exames_complementares", label: "Exames complementares" },
  { value: "medicacoes", label: "Medicações" },
  { value: "alergias", label: "Alergias" },
  { value: "pendencia", label: "Pendências" },
];
const TARGET_LABEL = Object.fromEntries(TARGETS.map((t) => [t.value, t.label]));
const EXAMPLES = ["Qual o último potássio?", "Posso usar AINE?", "O que mudou desde a última consulta?", "Dose de metformina pela TFG"];
const DISCUSS = "Quais os alertas críticos deste paciente e o que eles envolvem?";

/** Copiloto "Discutir o caso" / "Perguntar ao prontuário": cada frase traz a fonte (fala, FHIR ou regra). */
export function CopilotPanel({
  consultId,
  patientId,
  insights,
  disabled,
  pushed,
  ptt,
}: {
  consultId: number;
  patientId: string;
  insights: ConsultInsights | null;
  disabled?: boolean;
  /** pergunta ditada em Alt+Q: chega aqui, nunca na transcrição */
  pushed: { texto: string; n: number } | null;
  ptt: { active: boolean; interim: string; error: string | null; supported: boolean; start: () => void; stop: () => void };
}) {
  const [messages, setMessages] = useState<CopilotMessage[]>([]);
  const [inserted, setInserted] = useState<CopilotInsertion[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [target, setTarget] = useState<Record<number, string>>({});
  const bottom = useRef<HTMLDivElement>(null);
  const lastPushed = useRef(0);

  useEffect(() => {
    let alive = true;
    api<CopilotState>(`/consultations/${consultId}/copiloto`)
      .then((s) => {
        if (!alive) return;
        setMessages(s.mensagens);
        setInserted(s.insercoes);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [consultId]);

  useEffect(() => bottom.current?.scrollIntoView?.({ block: "nearest" }), [messages.length]);

  const ask = useCallback(
    async (texto: string, via: "texto" | "voz" = "texto") => {
      const q = texto.trim();
      if (!q) return;
      setBusy(true);
      setError(null);
      try {
        const res = await api<{ mensagem: CopilotMessage; insercoes: CopilotInsertion[] }>(`/consultations/${consultId}/ask`, {
          method: "POST",
          json: { pergunta: q, via },
        });
        setMessages((prev) => [...prev, res.mensagem]);
        setInserted(res.insercoes);
        setQuestion("");
      } catch (e) {
        setError(e instanceof Error ? e.message : "Erro ao consultar o copiloto");
      } finally {
        setBusy(false);
      }
    },
    [consultId],
  );

  useEffect(() => {
    if (pushed && pushed.n !== lastPushed.current) {
      lastPushed.current = pushed.n;
      void ask(pushed.texto, "voz");
    }
  }, [pushed, ask]);

  async function insert(m: CopilotMessage, destino: string) {
    setError(null);
    try {
      const res = await api<{ insercoes: CopilotInsertion[] }>(`/consultations/${consultId}/copiloto/inserir`, {
        method: "POST",
        json: { mensagem_id: m.id, destino },
      });
      setInserted(res.insercoes);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao inserir na nota");
    }
  }

  const critical = insights?.alertas.find((a) => a.severidade === "critico");
  const insertedFor = (id: number) => inserted.filter((i) => i.mensagem_id === id);

  return (
    <Card aria-labelledby="copilot-title">
      <CardHeader>
        <CardTitle id="copilot-title" className="flex flex-wrap items-center gap-2">
          <MessageCircleQuestion aria-hidden className="size-5 text-primary" /> Copiloto do caso
          <Badge variant="outline">fonte em cada frase</Badge>
        </CardTitle>
        <CardDescription>
          Pergunte ao prontuário e à conversa. Cálculos e alertas vêm das regras do HousIA; a IA só cita, e frase sem fonte válida é
          descartada. Segure <kbd className="rounded border px-1 font-mono text-xs">Alt+Q</kbd> para perguntar por voz (não entra na
          transcrição).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {critical && !dismissed && (
          <Alert variant="critico" role="status">
            <div className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1">
                <span className="font-bold">Alerta crítico:</span> {critical.texto}
              </span>
              <Button size="sm" variant="outline" onClick={() => void ask(DISCUSS)} disabled={busy || disabled}>
                Discutir isto?
              </Button>
              <button className="text-xs underline" onClick={() => setDismissed(true)}>
                agora não
              </button>
            </div>
          </Alert>
        )}

        <div className="max-h-96 space-y-3 overflow-y-auto pr-1" role="log" aria-live="polite" aria-label="Conversa com o copiloto">
          {messages.length === 0 && (
            <div className="space-y-2 text-sm text-muted-foreground">
              <p>Exemplos:</p>
              <div className="flex flex-wrap gap-1.5">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => void ask(ex)}
                    disabled={busy || disabled}
                    className="min-h-9 rounded-full border bg-card px-3 text-sm hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {ex}
                  </button>
                ))}
              </div>
              <p className="text-xs">
                Também entendo comandos: “adicionar ao plano …” e “criar pendência …”.
              </p>
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} className="space-y-1.5">
              <p className="ml-auto w-fit max-w-[90%] rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground">
                {m.via === "voz" && <Mic aria-label="por voz" className="mr-1 inline size-3.5" />}
                {m.pergunta}
              </p>
              <div className="max-w-full space-y-1.5 rounded-lg border bg-card p-3 text-sm">
                {m.resposta.frases.map((f, i) => (
                  <p key={i} className={cn(i === 0 && m.resposta.frases.length > 1 && "font-bold")}>
                    {f.texto}{" "}
                    <SourceRefs patientId={patientId} refs={f.refs} />
                    {f.fonte && <span className="block text-xs text-muted-foreground">Fonte da regra: {f.fonte}</span>}
                    {f.origem === "ia" && <Badge variant="info">IA</Badge>}
                  </p>
                ))}
                {m.resposta.aviso && <p className="text-muted-foreground">{m.resposta.aviso}</p>}
                {m.resposta.descartados.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {m.resposta.descartados.length} frase(s) da IA descartada(s) por falta de fonte válida.
                  </p>
                )}
                {m.resposta.frases.length > 0 && !m.resposta.acao && (
                  <div className="flex flex-wrap items-center gap-2 border-t pt-2">
                    <label className="inline-flex items-center gap-1.5 text-xs">
                      <span className="sr-only sm:not-sr-only">Seção</span>
                      <NativeSelect
                        aria-label={`Seção da nota para a resposta ${m.id}`}
                        value={target[m.id] ?? "avaliacao"}
                        onChange={(e) => setTarget((t) => ({ ...t, [m.id]: e.target.value }))}
                        className="h-9 w-44"
                      >
                        {TARGETS.map((t) => (
                          <option key={t.value} value={t.value}>
                            {t.label}
                          </option>
                        ))}
                      </NativeSelect>
                    </label>
                    <Button size="sm" variant="outline" disabled={disabled} onClick={() => void insert(m, target[m.id] ?? "avaliacao")}>
                      <FilePlus2 aria-hidden /> Inserir na nota
                    </Button>
                    <Button size="sm" variant="ghost" disabled={disabled} onClick={() => void insert(m, "plano")}>
                      <ListPlus aria-hidden /> Adicionar ao plano
                    </Button>
                    <Button size="sm" variant="ghost" disabled={disabled} onClick={() => void insert(m, "pendencia")}>
                      <ClipboardList aria-hidden /> Criar pendência
                    </Button>
                  </div>
                )}
                {insertedFor(m.id).map((i) => (
                  <p key={i.id} className="text-xs text-ok">
                    Na nota ({TARGET_LABEL[i.destino] ?? i.destino}) com {i.refs.length} fonte(s), origem copiloto.
                  </p>
                ))}
              </div>
            </div>
          ))}
          {busy && (
            <p className="inline-flex items-center gap-1 text-sm text-muted-foreground" role="status">
              <Loader2 aria-hidden className="size-4 animate-spin" /> Consultando o prontuário…
            </p>
          )}
          <div ref={bottom} />
        </div>

        {(error || ptt.error) && (
          <Alert variant="critico" role="alert">
            {error ?? ptt.error}
          </Alert>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask(question);
          }}
          className="flex gap-2"
        >
          <Input
            value={ptt.active ? ptt.interim : question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={ptt.active ? "Ouvindo…" : "Pergunte ao prontuário…"}
            aria-label="Pergunta ao copiloto"
            maxLength={500}
            disabled={disabled}
          />
          <Button
            type="button"
            variant={ptt.active ? "destructive" : "outline"}
            aria-label="Segurar para perguntar por voz (Alt+Q)"
            aria-keyshortcuts="Alt+Q"
            aria-pressed={ptt.active}
            disabled={disabled || !ptt.supported}
            onPointerDown={ptt.start}
            onPointerUp={ptt.stop}
            onPointerLeave={() => ptt.active && ptt.stop()}
            onKeyDown={(e) => {
              if ((e.key === " " || e.key === "Enter") && !e.repeat) ptt.start();
            }}
            onKeyUp={(e) => (e.key === " " || e.key === "Enter") && ptt.stop()}
          >
            <Mic aria-hidden />
          </Button>
          <Button type="submit" disabled={!question.trim() || busy || disabled}>
            <Send aria-hidden /> Perguntar
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
