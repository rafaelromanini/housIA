"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, FileText, Loader2, Lock } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, downloadFile } from "@/lib/api";

type DocItem = { id: string; texto: string; refs: string[]; origem: string; obrigatorio: boolean };
type DocSection = { titulo: string; itens: DocItem[] };
type Doc = {
  tipo: string;
  titulo: string;
  secoes: DocSection[];
  descartados: { secao: string; texto: string; motivo: string }[];
  avisos: string[];
  aprovado: { por: string; em: string } | null;
  fhir_ref: string | null;
  desatualizado: boolean;
  texto: string;
  legibilidade?: { indice: number | null; faixa: string; escolaridade: string };
};
type Row = { tipo: string; titulo: string; gerado: boolean; indisponivel: string | null; desatualizado: boolean; aprovado: boolean; enviado: boolean };

function reason(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "mensagem" in detail) return String((detail as { mensagem: unknown }).mensagem);
  }
  return e instanceof Error ? e.message : fallback;
}

/** Aba "Documentos": atestado, encaminhamento, contrarreferência, convênio, receita, exames e o resumo ao paciente. */
export function DocumentsPanel({ consultId, patientId, ready }: { consultId: number; patientId: string; ready: boolean }) {
  const base = `/consultations/${consultId}/documentos`;
  const [rows, setRows] = useState<Row[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [dias, setDias] = useState("");
  const [incluirCid, setIncluirCid] = useState(false);
  const [autorizaCid, setAutorizaCid] = useState(false);
  const [edits, setEdits] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      setRows((await api<{ documentos: Row[] }>(base)).documentos);
    } catch {
      setRows([]);
    }
  }, [base]);

  useEffect(() => {
    if (ready) void refresh();
  }, [ready, refresh]);

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | null> {
    setBusy(label);
    setError(null);
    setMessage(null);
    try {
      return await fn();
    } catch (e) {
      setError(reason(e, "Não foi possível concluir"));
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function show(tipo: string) {
    setOpen(tipo);
    setEdits({});
    const got = await run("ver", () => api<Doc>(`${base}/${tipo}`).catch(() => null));
    setDoc(got);
  }

  async function generate(tipo: string) {
    const json = tipo === "atestado" || tipo === "relatorio_convenio"
      ? { dias: dias ? Number(dias) : null, incluir_cid: incluirCid, autorizacao_cid: autorizaCid }
      : undefined;
    const out = await run("gerar", () => api<Doc>(`${base}/${tipo}`, { method: "POST", json }));
    if (out) {
      setOpen(tipo);
      setDoc(out);
      setEdits({});
      void refresh();
    }
  }

  async function saveEdits() {
    if (!doc) return;
    const secoes = doc.secoes.map((s) => ({ ...s, itens: s.itens.map((i) => ({ ...i, texto: edits[i.id] ?? i.texto })) }));
    const out = await run("salvar", () => api<Doc>(`${base}/${doc.tipo}`, { method: "PUT", json: { secoes } }));
    if (out) {
      setDoc(out);
      setEdits({});
      setMessage("Edição salva. Aprove de novo para exportar.");
      void refresh();
    }
  }

  async function approve() {
    if (!doc) return;
    const out = await run("aprovar", () => api<Doc>(`${base}/${doc.tipo}/aprovar`, { method: "POST" }));
    if (out) {
      setDoc(out);
      setMessage("Documento aprovado por você.");
      void refresh();
    }
  }

  async function copy() {
    if (!doc) return;
    const out = await run("copiar", () => api<{ texto: string }>(`${base}/${doc.tipo}/export`));
    if (out) {
      await navigator.clipboard.writeText(out.texto);
      setMessage("Texto copiado.");
    }
  }

  async function send() {
    if (!doc) return;
    const out = await run("enviar", () => api<{ enviado: boolean; fhir_ref?: string }>(`${base}/${doc.tipo}/send`, { method: "POST" }));
    if (out) {
      setMessage(out.enviado ? `Enviado ao prontuário (${out.fhir_ref}).` : "Envio automático desligado: copie o texto ou baixe o PDF.");
      void refresh();
    }
  }

  if (!ready) return null;
  const dirty = Object.keys(edits).length > 0;
  return (
    <section aria-labelledby="documentos-consulta" data-testid="documentos-consulta" className="space-y-2 rounded-md border p-3">
      <h3 id="documentos-consulta" className="flex flex-wrap items-center gap-2 text-sm font-bold">
        <FileText aria-hidden className="size-4 text-primary" /> Documentos
        {busy && <Loader2 aria-hidden className="size-4 animate-spin" />}
      </h3>
      <p className="text-xs text-muted-foreground">
        Gerados da nota revisada, dos pedidos que você assinou e do prontuário; cada linha cita a fonte. Nada vai ao prontuário sem a sua aprovação.
      </p>
      {error && <Alert variant="critico">{error}</Alert>}
      {message && <Alert variant="info">{message}</Alert>}
      <ul className="grid gap-1.5 sm:grid-cols-2">
        {rows.map((r) => (
          <li key={r.tipo} className="flex items-center justify-between gap-2 rounded-md bg-secondary/40 p-2">
            <span className="min-w-0 text-sm">
              <b>{r.titulo}</b>
              {r.indisponivel && <span className="block text-xs text-muted-foreground">{r.indisponivel}</span>}
              {r.desatualizado && <span className="block text-xs text-amber-700">A nota mudou: gere de novo.</span>}
            </span>
            <span className="flex shrink-0 items-center gap-1.5">
              {r.enviado ? <Badge variant="outline">enviado</Badge> : r.aprovado ? <Badge variant="outline">aprovado</Badge> : null}
              {r.gerado && (
                <Button size="sm" variant="outline" onClick={() => void show(r.tipo)} disabled={busy !== null}>
                  Abrir
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => void generate(r.tipo)} disabled={busy !== null || !!r.indisponivel || r.enviado}>
                {r.gerado ? "Gerar de novo" : "Gerar"}
              </Button>
            </span>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-end gap-3 rounded-md border p-2 text-xs">
        <span className="font-bold">Atestado e convênio</span>
        <div>
          <Label htmlFor="doc-dias">Dias de afastamento</Label>
          <Input id="doc-dias" inputMode="numeric" className="h-8 w-24" value={dias} onChange={(e) => setDias(e.target.value.replace(/\D/g, ""))} />
        </div>
        <Label className="flex items-center gap-1.5">
          <Checkbox checked={incluirCid} onCheckedChange={(v) => setIncluirCid(v === true)} /> Incluir o CID
        </Label>
        <Label className="flex items-center gap-1.5">
          <Checkbox checked={autorizaCid} onCheckedChange={(v) => setAutorizaCid(v === true)} /> O paciente autorizou informar o CID
        </Label>
      </div>
      {open && doc && doc.tipo === open && (
        <div className="space-y-2 rounded-md border p-3" data-testid="documento-aberto">
          <h4 className="flex flex-wrap items-center gap-2 text-sm font-bold">
            {doc.titulo}
            {doc.aprovado && (
              <Badge variant="outline">
                <CheckCircle2 aria-hidden className="size-3" /> aprovado por {doc.aprovado.por}
              </Badge>
            )}
            {doc.legibilidade?.indice != null && (
              <Badge variant="outline">
                leitura {doc.legibilidade.faixa} ({doc.legibilidade.indice}) · {doc.legibilidade.escolaridade}
              </Badge>
            )}
          </h4>
          {doc.avisos.map((a) => (
            <Alert key={a} variant="atencao">{a}</Alert>
          ))}
          {doc.secoes.map((s) => (
            <div key={s.titulo}>
              <h5 className="text-xs font-bold uppercase text-muted-foreground">{s.titulo}</h5>
              <ul className="space-y-1.5">
                {s.itens.map((i) => (
                  <li key={i.id} className="space-y-0.5">
                    <Label htmlFor={`doc-${i.id}`} className="sr-only">Texto do item</Label>
                    <Textarea
                      id={`doc-${i.id}`}
                      rows={2}
                      value={edits[i.id] ?? i.texto}
                      disabled={!!doc.fhir_ref}
                      onChange={(e) => setEdits((cur) => ({ ...cur, [i.id]: e.target.value }))}
                    />
                    <span className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                      {i.obrigatorio && (
                        <Badge variant="outline"><Lock aria-hidden className="size-3" /> segurança: não sai</Badge>
                      )}
                      <Badge variant="outline">{i.origem}</Badge>
                      <SourceRefs patientId={patientId} refs={i.refs} />
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {doc.descartados.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Ficaram de fora: {doc.descartados.map((d) => `${d.texto.slice(0, 50)} (${d.motivo})`).join("; ")}.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => void saveEdits()} disabled={!dirty || busy !== null}>Salvar edição</Button>
            <Button size="sm" onClick={() => void approve()} disabled={busy !== null || dirty || !!doc.aprovado || doc.desatualizado}>Aprovar documento</Button>
            <Button size="sm" variant="outline" onClick={() => void copy()} disabled={busy !== null || !doc.aprovado}>Copiar texto</Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy !== null || !doc.aprovado}
              onClick={() => void run("pdf", () => downloadFile(`${base}/${doc.tipo}/export.pdf`, `${doc.tipo}-${consultId}.pdf`))}
            >
              Baixar PDF
            </Button>
            <Button size="sm" variant="outline" onClick={() => void send()} disabled={busy !== null || !doc.aprovado || !!doc.fhir_ref}>
              Enviar ao prontuário
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
