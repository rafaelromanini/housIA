"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Square, Trash2 } from "lucide-react";

import { PCM_WORKLET } from "@/components/consult/capture";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { api, type VoiceProfile } from "@/lib/api";
import { useApi } from "@/lib/use-api";

const MIN_S = 10;
const MAX_S = 20;
const TAXA = 16000;

/** PCM int16 mono 16 kHz para WAV (cabeçalho de 44 bytes). O áudio só existe em memória até o envio. */
function toWav(blocks: Int16Array[]): Blob {
  const total = blocks.reduce((n, b) => n + b.length, 0);
  const buf = new ArrayBuffer(44 + total * 2);
  const v = new DataView(buf);
  const txt = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  txt(0, "RIFF");
  v.setUint32(4, 36 + total * 2, true);
  txt(8, "WAVEfmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, TAXA, true);
  v.setUint32(28, TAXA * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  txt(36, "data");
  v.setUint32(40, total * 2, true);
  let o = 44;
  for (const b of blocks) {
    for (let i = 0; i < b.length; i++, o += 2) v.setInt16(o, b[i], true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** Cadastro da voz do próprio profissional (#40): consentimento, gravação de 10 a 20 s e apagar. */
export function VoiceProfileView() {
  const perfil = useApi<VoiceProfile>("/consult/perfil-voz");
  const [aceite, setAceite] = useState(false);
  const [gravando, setGravando] = useState(false);
  const [segundos, setSegundos] = useState(0);
  const [msg, setMsg] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const blocos = useRef<Int16Array[]>([]);
  const media = useRef<MediaStream | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const timer = useRef<number | null>(null);
  const segundosRef = useRef(0);
  const aceiteRef = useRef(false);
  aceiteRef.current = aceite;

  const liberar = useCallback(() => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    media.current?.getTracks().forEach((t) => t.stop());
    media.current = null;
    void ctx.current?.close();
    ctx.current = null;
    setGravando(false);
  }, []);

  useEffect(() => liberar, [liberar]);

  const parar = useCallback(async () => {
    const ok = segundosRef.current >= MIN_S;
    const havia = blocos.current.length > 0; // um 2º "parar" (clique junto do fim dos 20 s) não reenvia nada
    liberar();
    if (!havia) return;
    if (!aceiteRef.current) {
      blocos.current = []; // sem o aceite vigente nenhum áudio sai do navegador
      return;
    }
    if (!ok) {
      blocos.current = [];
      setErro(`Grave ao menos ${MIN_S} segundos falando.`);
      return;
    }
    const versao = perfil.data?.termo_versao;
    if (!versao) return;
    const wav = toWav(blocos.current);
    blocos.current = [];
    setBusy(true);
    try {
      const novo = await api<VoiceProfile>("/consult/perfil-voz", {
        method: "PUT",
        body: wav,
        headers: { "Content-Type": "audio/wav", "X-Consentimento-Voz": versao },
      });
      perfil.setData(novo);
      setMsg("Voz cadastrada. Só o vetor foi guardado; o áudio foi descartado.");
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Não foi possível cadastrar a voz.");
    } finally {
      setBusy(false);
    }
  }, [liberar, perfil]);

  async function gravar() {
    if (!perfil.data) return;
    setErro(null);
    setMsg(null);
    blocos.current = [];
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } });
    } catch {
      setErro("Não foi possível acessar o microfone.");
      return;
    }
    media.current = stream;
    try {
      const audio = new AudioContext();
      ctx.current = audio;
      const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: "text/javascript" }));
      await audio.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      const node = new AudioWorkletNode(audio, "pcm16");
      node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => blocos.current.push(new Int16Array(e.data));
      audio.createMediaStreamSource(stream).connect(node);
    } catch {
      setErro("Este navegador não consegue capturar áudio para o cadastro.");
      liberar();
      return;
    }
    segundosRef.current = 0;
    setSegundos(0);
    setGravando(true);
    timer.current = window.setInterval(() => {
      segundosRef.current += 1;
      setSegundos(segundosRef.current);
      if (segundosRef.current >= MAX_S) void parar();
    }, 1000);
  }

  async function apagar() {
    setBusy(true);
    setErro(null);
    try {
      perfil.setData(await api<VoiceProfile>("/consult/perfil-voz", { method: "DELETE" }));
      setMsg("Perfil de voz apagado.");
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Não foi possível apagar.");
    } finally {
      setBusy(false);
    }
  }

  const p = perfil.data;
  return (
    <div className="mx-auto w-full max-w-2xl space-y-4 p-4">
      <h1 className="text-xl font-bold">Minha voz</h1>
      <Card>
        <CardHeader>
          <CardTitle>Reconhecer quando eu falo na consulta</CardTitle>
          <CardDescription>
            A voz cadastrada permite separar, no áudio do servidor, o que é do profissional e o que é do paciente. Sem ela, o botão de
            quem fala continua funcionando.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {perfil.error && <Alert variant="critico">{perfil.error}</Alert>}
          {p && !p.diarizacao_ativa && (
            <Alert variant="info">
              A separação de falantes por voz está desligada neste servidor (recurso não habilitado pela equipe de TI). O cadastro fica indisponível.
            </Alert>
          )}
          {p && p.diarizacao_ativa && !p.cifrado && (
            <Alert variant="atencao">
              Este servidor está sem chave de criptografia de campo (DATA_ENCRYPTION_KEY): o vetor da voz ficaria sem cifra. Peça ao
              administrador antes de cadastrar.
            </Alert>
          )}
          {p?.cadastrado && (
            <Alert variant={p.compativel ? "ok" : "atencao"} data-testid="voz-cadastrada">
              {p.compativel
                ? `Voz cadastrada${p.atualizado_em ? ` em ${new Date(p.atualizado_em).toLocaleDateString("pt-BR")}` : ""}.`
                : "A voz cadastrada é de outro motor de voz e não está em uso. Cadastre de novo."}
            </Alert>
          )}
          {p && (
            <div className="rounded-md border bg-secondary/40 p-3 text-sm" data-testid="termo-voz">
              <p className="font-semibold">Termo de consentimento (dado biométrico)</p>
              <p className="mt-1">{p.termo}</p>
              <div className="mt-3 flex items-start gap-2">
                <Checkbox id="aceite-voz" checked={aceite} onCheckedChange={(v) => setAceite(v === true)} disabled={gravando} />
                <Label htmlFor="aceite-voz">Li e aceito o uso da minha voz nestas condições.</Label>
              </div>
            </div>
          )}
          <p className="text-sm text-muted-foreground">
            Grave de {MIN_S} a {MAX_S} segundos lendo qualquer texto, sozinho(a) e em ambiente silencioso. O áudio não é guardado.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {!gravando ? (
              <Button onClick={() => void gravar()} disabled={!p?.diarizacao_ativa || !aceite || busy}>
                <Mic aria-hidden /> {p?.cadastrado ? "Gravar de novo" : "Gravar e cadastrar"}
              </Button>
            ) : (
              <Button variant="secondary" onClick={() => void parar()}>
                <Square aria-hidden /> Parar e enviar ({segundos}s)
              </Button>
            )}
            {p?.cadastrado && (
              <Button variant="outline" onClick={() => void apagar()} disabled={busy || gravando}>
                <Trash2 aria-hidden /> Apagar meu perfil de voz
              </Button>
            )}
          </div>
          {msg && (
            <p className="text-sm text-ok" role="status">
              {msg}
            </p>
          )}
          {erro && <Alert variant="critico">{erro}</Alert>}
        </CardContent>
      </Card>
    </div>
  );
}
