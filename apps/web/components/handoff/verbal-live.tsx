"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CircleCheck, Circle, Loader2, Mic, Play, Square } from "lucide-react";

import { speechSupported, useServerStream, useSpeechCapture } from "@/components/consult/capture";
import { ResultadoConferencia } from "@/components/handoff/resultado-conferencia";
import { usePatient } from "@/components/patient/patient-context";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, ApiError, type Audience, type ConsultConfig, type HandoffCheck, type SbarStep } from "@/lib/api";
import { cn } from "@/lib/utils";

type Fonte = "servidor" | "navegador" | "demo" | "digitar";
type Fala = { n: number; texto: string; origem: "servidor" | "navegador" | "demo" | "digitado" };
type DemoScript = { id: string; titulo: string; falas: { texto: string }[] };

function mensagem(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : "Erro na conferência";
}

const ESTADO: Record<SbarStep["estado"], string> = {
  completo: "Dito",
  falta_dizer: "Falta dizer",
  a_dizer: "A dizer",
};

/** Passagem falada: a conferência roda a cada fala, com o roteiro SBAR guiando quem fala. */
export function VerbalLive({ audience, windowH }: { audience: Audience; windowH: number }) {
  const { patient } = usePatient();
  const [config, setConfig] = useState<ConsultConfig | null>(null);
  const [fonte, setFonte] = useState<Fonte>("digitar");
  const [ciente, setCiente] = useState(false);
  const [falas, setFalas] = useState<Fala[]>([]);
  const [ao_vivo, setAoVivo] = useState<HandoffCheck | null>(null);
  const [final, setFinal] = useState<HandoffCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [texto, setTexto] = useState("");
  const [demo, setDemo] = useState<DemoScript | null>(null);
  const [tocando, setTocando] = useState(false);
  const [parcial, setParcial] = useState("");
  const falasRef = useRef<Fala[]>([]);
  const seq = useRef(0);
  const escolhida = useRef(false); // a escolha da pessoa vale mais que o padrão que chega depois da configuração

  useEffect(() => {
    void api<ConsultConfig>("/consult/config")
      .then((c) => {
        setConfig(c);
        if (!escolhida.current) setFonte(c.stt_streaming ? "servidor" : speechSupported() ? "navegador" : "digitar");
      })
      .catch(() => setConfig(null));
  }, []);

  useEffect(() => {
    void api<{ id: string; patient_id: string; audience: Audience }[]>("/handoff/demo-scripts")
      .then(async (all) => {
        const mine = all.find((s) => s.patient_id === patient.id && s.audience === audience);
        setDemo(mine ? await api<DemoScript>(`/handoff/demo-scripts/${mine.id}`) : null);
      })
      .catch(() => setDemo(null));
  }, [patient.id, audience]);

  const corpo = useCallback(
    (lista: Fala[], fim: boolean) => ({
      audience,
      window_hours: windowH,
      final: fim,
      ciente: true,
      segmentos: lista.map((f) => ({ n: f.n, texto: f.texto, origem: f.origem })),
    }),
    [audience, windowH],
  );

  const conferir = useCallback(
    async (lista: Fala[]) => {
      const mine = ++seq.current;
      try {
        const out = await api<HandoffCheck>(`/patients/${patient.id}/handoff/verbal-check/segments`, {
          method: "POST",
          json: corpo(lista, false),
        });
        if (mine === seq.current) setAoVivo(out); // resposta antiga não pisa na mais nova
      } catch (e) {
        setError(mensagem(e));
      }
    },
    [patient.id, corpo],
  );

  const acrescentar = useCallback(
    (texto_: string, origem: Fala["origem"]) => {
      const t = texto_.trim();
      if (!t) return;
      const next = [...falasRef.current, { n: falasRef.current.length + 1, texto: t, origem }];
      falasRef.current = next;
      setFalas(next);
      setFinal(null);
      void conferir(next);
    },
    [conferir],
  );

  const navegador = useSpeechCapture("pt-BR", (t) => acrescentar(t, "navegador"));
  const servidor = useServerStream(0, "medico", () => {}, {
    url: `/ws/patients/${patient.id}/handoff/audio?audience=${audience}&janela_h=${windowH}&ciente=true`,
    onMessage: (m) => {
      if (m.tipo === "parcial") {
        setParcial(String(m.texto ?? ""));
        setAoVivo(m.conferencia as HandoffCheck);
      } else if (m.tipo === "final") {
        const f = m.fala as { n: number; texto: string };
        const next = [...falasRef.current, { ...f, origem: "servidor" as const }];
        falasRef.current = next;
        setFalas(next);
        setParcial("");
        setFinal(null);
        setAoVivo(m.conferencia as HandoffCheck);
      }
    },
  });

  // roteiro de demonstração: uma fala a cada 2,5 s
  useEffect(() => {
    if (!tocando || !demo) return;
    const i = falasRef.current.length;
    if (i >= demo.falas.length) {
      setTocando(false);
      return;
    }
    const t = window.setTimeout(() => acrescentar(demo.falas[i].texto, "demo"), 2500);
    return () => window.clearTimeout(t);
  }, [tocando, demo, falas.length, acrescentar]);

  function reiniciar() {
    falasRef.current = [];
    setFalas([]);
    setAoVivo(null);
    setFinal(null);
    setParcial("");
    setError(null);
    seq.current++;
  }

  async function iniciar() {
    setError(null);
    if (fonte === "servidor") await servidor.start();
    else if (fonte === "navegador") navegador.start();
    else if (fonte === "demo") setTocando(true);
  }

  async function encerrar() {
    setBusy(true);
    setError(null);
    try {
      setTocando(false);
      if (fonte === "servidor") await servidor.stop();
      if (fonte === "navegador") await navegador.stop();
      if (falasRef.current.length === 0) {
        setError("Ninguém falou ainda.");
        return;
      }
      seq.current++;
      setFinal(
        await api<HandoffCheck>(`/patients/${patient.id}/handoff/verbal-check/segments`, {
          method: "POST",
          json: corpo(falasRef.current, true),
        }),
      );
    } catch (e) {
      setError(mensagem(e));
    } finally {
      setBusy(false);
    }
  }

  const capturando = servidor.streaming || navegador.listening || tocando;
  const resultado = final ?? ao_vivo;
  const proximo = resultado?.roteiro?.find((e) => e.estado !== "completo");
  const erroCaptura = servidor.error ?? navegador.error;

  return (
    <div className="space-y-3" data-testid="passagem-ao-vivo">
      <div className="flex items-start gap-2">
        <Checkbox id="ciente-voz" checked={ciente} onCheckedChange={(v) => setCiente(v === true)} />
        <Label htmlFor="ciente-voz" className="text-sm font-normal">
          Estou ciente de que a minha fala é transcrita e conferida com o prontuário. O áudio e o texto não ficam gravados;
          a auditoria guarda só as contagens.
        </Label>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor="fonte-voz">Captura</Label>
          <NativeSelect id="fonte-voz" value={fonte} disabled={capturando} onChange={(e) => {
              escolhida.current = true;
              setFonte(e.target.value as Fonte);
            }}>
            {config?.stt_streaming && <option value="servidor">Microfone (servidor)</option>}
            {speechSupported() && <option value="navegador">Microfone (navegador, demonstração)</option>}
            {demo && <option value="demo">Roteiro de demonstração</option>}
            <option value="digitar">Digitar cada fala</option>
          </NativeSelect>
        </div>
        {fonte !== "digitar" && !capturando && (
          <Button onClick={() => void iniciar()} disabled={!ciente || busy}>
            {fonte === "demo" ? <Play aria-hidden /> : <Mic aria-hidden />}
            {fonte === "demo" ? "Tocar roteiro" : "Começar a falar"}
          </Button>
        )}
        {capturando && (
          <Button variant="outline" onClick={() => (fonte === "demo" ? setTocando(false) : void encerrar())}>
            <Square aria-hidden /> Pausar
          </Button>
        )}
        <Button variant="accent" onClick={() => void encerrar()} disabled={!ciente || busy || falas.length === 0}>
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : <CircleCheck aria-hidden />}
          Encerrar e conferir
        </Button>
        {falas.length > 0 && !busy && (
          <Button variant="ghost" onClick={reiniciar}>
            Recomeçar
          </Button>
        )}
      </div>
      {fonte === "navegador" && (
        <Alert variant="atencao">
          O reconhecimento do navegador envia o áudio ao serviço de fala do navegador: use só para demonstração.
        </Alert>
      )}
      {fonte === "digitar" && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            acrescentar(texto, "digitado");
            setTexto("");
          }}
        >
          <Input
            aria-label="Próxima fala da passagem"
            value={texto}
            disabled={!ciente}
            onChange={(e) => setTexto(e.target.value)}
            placeholder="Ex.: PA estável, sem intercorrências"
          />
          <Button type="submit" variant="outline" disabled={!ciente || !texto.trim()}>
            Enviar fala
          </Button>
        </form>
      )}
      {(error || erroCaptura) && <Alert variant="critico">{error ?? erroCaptura}</Alert>}

      {proximo && (
        <p className="rounded-md border bg-muted/40 p-2 text-sm" data-testid="guia-sbar">
          <span className="font-bold">
            Próximo, {proximo.sigla} — {proximo.titulo}:
          </span>{" "}
          {proximo.guia}
        </p>
      )}
      {resultado?.roteiro && (
        <ol className="grid gap-2 sm:grid-cols-4" aria-label="Roteiro SBAR">
          {resultado.roteiro.map((e) => (
            <li
              key={e.sigla}
              data-testid={`sbar-${e.sigla}`}
              className={cn("rounded-md border p-2 text-sm", e.estado === "falta_dizer" && "border-atencao-border")}
            >
              <div className="flex items-center gap-1.5 font-bold">
                {e.estado === "completo" ? <CircleCheck aria-hidden className="size-4 text-ok" /> : <Circle aria-hidden className="size-4" />}
                {e.sigla} · {e.titulo}
              </div>
              <Badge variant={e.estado === "completo" ? "ok" : e.estado === "falta_dizer" ? "atencao" : "outline"}>{ESTADO[e.estado]}</Badge>
              {e.faltam.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Falta: {e.faltam.length} item(ns)</p>}
            </li>
          ))}
        </ol>
      )}

      {falas.length > 0 && (
        <ol className="max-h-40 space-y-1 overflow-auto rounded-md border p-2 text-sm" aria-label="O que foi dito" aria-live="polite">
          {falas.map((f) => (
            <li key={f.n}>
              <span className="text-xs text-muted-foreground">Fala {f.n}</span> {f.texto}
            </li>
          ))}
          {parcial && <li className="italic text-muted-foreground">{parcial}…</li>}
        </ol>
      )}
      {resultado && <ResultadoConferencia result={resultado} parcial={!final} />}
    </div>
  );
}
