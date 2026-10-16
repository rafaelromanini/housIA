"use client";

import { useState } from "react";
import { Copy, MessageSquareText, PhoneCall } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, type ContactAnswer, type ContactQuestion, type ContactResult, type MessageDraft } from "@/lib/api";
import { useApi } from "@/lib/use-api";

const GROUPS: { tipo: ContactQuestion["tipo"]; titulo: string; ajuda: string }[] = [
  { tipo: "confirmacao", titulo: "Identificação", ajuda: "Confirme com quem está falando." },
  { tipo: "teach_back", titulo: "Teach-back", ajuda: "Peça para a pessoa explicar com as próprias palavras." },
  { tipo: "retirada", titulo: "Retirada dos remédios e retorno", ajuda: "Acesso aos medicamentos e à consulta de retorno." },
  { tipo: "sinal_alarme", titulo: "Sinais de alarme", ajuda: "Pergunte todos: uma resposta “sim” abre um alerta crítico." },
];
const ANSWERS: { value: ContactAnswer; label: string }[] = [
  { value: "sim", label: "Sim" },
  { value: "nao", label: "Não" },
  { value: "nao_se_aplica", label: "Não se aplica" },
];

/** Registro do contato pós-alta feito pela equipe (ligação, visita ou retorno de mensagem). Não envia nada ao paciente. */
export function ContactDialog({ patientId, patientName, onDone }: { patientId: string; patientName: string; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const script = useApi<{ perguntas: ContactQuestion[] }>(open ? `/journey/patients/${patientId}/contact-script` : null);
  const [canal, setCanal] = useState("telefone");
  const [atendeu, setAtendeu] = useState<boolean | null>(null);
  const [respostas, setRespostas] = useState<Record<string, ContactAnswer>>({});
  const [obs, setObs] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ContactResult | null>(null);

  const questions = script.data?.perguntas ?? [];
  const missingAlarms = questions.filter((q) => q.tipo === "sinal_alarme" && !respostas[q.id]);

  function reset(next: boolean) {
    setOpen(next);
    if (!next) {
      if (result) onDone();
      setResult(null);
      setAtendeu(null);
      setRespostas({});
      setObs("");
      setError(null);
    }
  }

  async function submit() {
    if (atendeu === null) {
      setError("Informe se você conseguiu falar com o paciente ou o cuidador.");
      return;
    }
    if (atendeu && missingAlarms.length > 0) {
      setError("Pergunte todos os sinais de alarme antes de registrar o contato.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api<ContactResult>(`/journey/patients/${patientId}/contacts`, {
          method: "POST",
          json: { canal, atendeu, respostas: atendeu ? respostas : {}, observacoes: obs.trim() || null },
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao registrar o contato");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <PhoneCall aria-hidden /> Registrar contato
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Contato pós-alta — {patientName}</DialogTitle>
          <DialogDescription>
            Registre o contato que a equipe fez. O HousIA não envia SMS nem WhatsApp. O registro vira uma nota no prontuário.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-3">
            {result.atendeu ? (
              <Alert variant="ok">Contato registrado e nota enviada ao prontuário ({result.fhir_ref}). O paciente saiu da lista de alta sem contato.</Alert>
            ) : (
              <Alert variant="atencao">Tentativa sem sucesso registrada. O paciente continua na lista até o contato acontecer.</Alert>
            )}
            {result.alerta && (
              <Alert variant="critico">
                <span>
                  <strong>Alerta crítico aberto:</strong> {result.alerta.titulo}. Avalie o paciente hoje.
                </span>
              </Alert>
            )}
            {result.pendencias.length > 0 && (
              <div>
                <p className="text-sm font-bold">Pendências para a equipe</p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {result.pendencias.map((p) => (
                    <li key={p.pergunta}>{p.texto}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="flex justify-end">
              <Button onClick={() => reset(false)}>Fechar</Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="contato-canal">Como foi o contato</Label>
                <NativeSelect id="contato-canal" value={canal} onChange={(e) => setCanal(e.target.value)}>
                  <option value="telefone">Ligação</option>
                  <option value="visita">Visita domiciliar</option>
                  <option value="presencial">Presencial</option>
                  <option value="mensagem">Resposta a mensagem</option>
                </NativeSelect>
              </div>
              <fieldset className="space-y-1.5">
                <legend className="text-sm font-bold">Conseguiu falar com o paciente ou o cuidador?</legend>
                <div className="flex gap-4 pt-1">
                  {[
                    { v: true, l: "Sim, falei" },
                    { v: false, l: "Não atendeu" },
                  ].map((o) => (
                    <label key={String(o.v)} className="inline-flex min-h-11 items-center gap-2 text-sm">
                      <input type="radio" name="atendeu" checked={atendeu === o.v} onChange={() => setAtendeu(o.v)} className="size-4" />
                      {o.l}
                    </label>
                  ))}
                </div>
              </fieldset>
            </div>

            {atendeu && script.loading && <Skeleton className="h-32 w-full" />}
            {atendeu &&
              GROUPS.map((g) => {
                const qs = questions.filter((q) => q.tipo === g.tipo);
                if (qs.length === 0) return null;
                return (
                  <section key={g.tipo} aria-label={g.titulo} className="space-y-2">
                    <div>
                      <h3 className="text-sm font-bold">{g.titulo}</h3>
                      <p className="text-xs text-muted-foreground">{g.ajuda}</p>
                    </div>
                    {qs.map((q) => (
                      <fieldset key={q.id} className="rounded-md border p-3">
                        <legend className="px-1 text-sm">{q.texto}</legend>
                        <div className="flex flex-wrap gap-x-4">
                          {ANSWERS.map((a) => (
                            <label key={a.value} className="inline-flex min-h-11 items-center gap-2 text-sm">
                              <input
                                type="radio"
                                name={q.id}
                                checked={respostas[q.id] === a.value}
                                onChange={() => setRespostas((r) => ({ ...r, [q.id]: a.value }))}
                                className="size-4"
                              />
                              {a.label}
                            </label>
                          ))}
                        </div>
                      </fieldset>
                    ))}
                  </section>
                );
              })}

            <div className="space-y-1.5">
              <Label htmlFor="contato-obs">Observações</Label>
              <Textarea id="contato-obs" value={obs} onChange={(e) => setObs(e.target.value)} maxLength={1000} />
            </div>
            {error && (
              <p role="alert" className="text-sm font-bold text-destructive">
                {error}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => reset(false)}>
                Cancelar
              </Button>
              <Button disabled={busy} onClick={() => void submit()}>
                Registrar contato
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Texto pronto de acompanhamento: a equipe copia e envia pelo canal oficial. O HousIA não envia. */
export function MessageDialog({ patientId, patientName }: { patientId: string; patientName: string }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<"ok" | "erro" | null>(null);
  const draft = useApi<MessageDraft>(open ? `/journey/patients/${patientId}/message-draft` : null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(draft.data?.texto ?? "");
      setCopied("ok");
    } catch {
      setCopied("erro");
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setCopied(null);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost">
          <MessageSquareText aria-hidden /> Texto pronto
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Texto de acompanhamento — {patientName}</DialogTitle>
          <DialogDescription>Revise antes de usar. Serve de roteiro de ligação ou de mensagem enviada pela equipe.</DialogDescription>
        </DialogHeader>
        {draft.loading && <Skeleton className="h-40 w-full" />}
        {draft.error && <Alert variant="critico">{draft.error}</Alert>}
        {draft.data && (
          <div className="space-y-3">
            <Alert variant="info">{draft.data.aviso}</Alert>
            <Label htmlFor="texto-pronto">Texto</Label>
            <Textarea id="texto-pronto" readOnly value={draft.data.texto} className="min-h-64 font-sans" />
            {draft.data.telefone && <p className="text-sm text-muted-foreground">Telefone cadastrado: {draft.data.telefone}</p>}
            <div className="flex items-center justify-end gap-3">
              <span role="status" className="text-sm font-bold">
                {copied === "ok" && "Texto copiado"}
                {copied === "erro" && "Não foi possível copiar: selecione o texto"}
              </span>
              <Button onClick={() => void copy()}>
                <Copy aria-hidden /> Copiar texto
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
