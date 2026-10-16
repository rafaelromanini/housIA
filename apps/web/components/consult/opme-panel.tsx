"use client";

import { useEffect, useRef, useState } from "react";
import { Camera, CameraOff, FileUp, Loader2, PackageCheck, Plus, ScanBarcode, Trash2 } from "lucide-react";

import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  type ConsultInsights,
  type Consultation,
  type OpmeCatalogItem,
  type OpmeGuideInfo,
  type OpmeLine,
  type OpmeScanResult,
  type OpmeVia,
  type SurgicalReport,
} from "@/lib/api";

export const OPME_STATUS: Record<OpmeLine["status"], { label: string; variant: "ok" | "critico" | "atencao" }> = {
  conferido: { label: "Conferido", variant: "ok" },
  nao_autorizado: { label: "Sem autorização", variant: "critico" },
  nao_citado: { label: "Autorizado, não citado", variant: "atencao" },
  quantidade_divergente: { label: "Quantidade divergente", variant: "atencao" },
  validade_vencida: { label: "Validade vencida", variant: "critico" },
  sem_etiqueta: { label: "Sem etiqueta lida", variant: "atencao" },
  sem_fala: { label: "Lido, não citado", variant: "atencao" },
};
const VIA: Record<OpmeVia, { label: string; variant: "ok" | "critico" | "atencao" | "outline" }> = {
  ok: { label: "ok", variant: "ok" },
  divergente: { label: "diverge", variant: "atencao" },
  ausente: { label: "ausente", variant: "critico" },
  nao_registrado: { label: "sem leitura", variant: "outline" },
  nao_aplica: { label: "não se aplica", variant: "outline" },
};

export const fmtDate = (iso: string) => iso.slice(0, 10).split("-").reverse().join("/");

type BarcodeDetectorLike = { detect(source: CanvasImageSource): Promise<{ rawValue: string }[]> };
type BarcodeDetectorCtor = new (options?: { formats?: string[] }) => BarcodeDetectorLike;

/** Tabela das três vias: o narrado (Fala/N), o autorizado (guia) e o consumido (etiqueta), cada um com seu status. */
export function OpmeTable({ lines, patientId }: { lines: OpmeLine[]; patientId: string }) {
  if (lines.length === 0) {
    return <p className="text-sm text-muted-foreground">Nenhum material citado, autorizado ou lido ainda.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">Materiais citados e autorizados</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-1 pr-2">Material</th>
            <th scope="col" className="py-1 pr-2">Narrado</th>
            <th scope="col" className="py-1 pr-2">Autorizado</th>
            <th scope="col" className="py-1 pr-2">Consumido (etiqueta)</th>
            <th scope="col" className="py-1">Situação</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((o) => (
            <tr key={o.codigo} className="border-b align-top">
              <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                <span className="font-bold">{o.descricao}</span>
                <span className="block font-mono text-xs text-muted-foreground">{o.codigo}</span>
                {o.tamanhos.length > 0 && <span className="block text-xs">Tamanho: {o.tamanhos.join("; ")}</span>}
                {o.implantavel && <span className="block text-xs text-muted-foreground">Implantável{o.registro_anvisa && ` · ${o.registro_anvisa}`}</span>}
              </th>
              <td className="py-1.5 pr-2">
                <span className="tabular-nums">{o.citado}</span> <Badge variant={VIA[o.status_fala].variant} className="text-[11px]">{VIA[o.status_fala].label}</Badge>
                <SourceRefs patientId={patientId} refs={o.refs} />
              </td>
              <td className="py-1.5 pr-2">
                <span className="tabular-nums">{o.autorizado}</span> <Badge variant={VIA[o.status_guia].variant} className="text-[11px]">{VIA[o.status_guia].label}</Badge>
              </td>
              <td className="py-1.5 pr-2">
                <span className="tabular-nums">{o.consumido}</span> <Badge variant={VIA[o.status_etiqueta].variant} className="text-[11px]">{VIA[o.status_etiqueta].label}</Badge>
                <ul className="mt-0.5 space-y-0.5 text-xs">
                  {o.lotes.map((l) => (
                    <li key={l.id} className={l.vencido ? "font-bold text-critico" : "text-muted-foreground"}>
                      Lote {l.lote ?? "—"}
                      {l.serie && ` · série ${l.serie}`}
                      {l.validade && ` · val. ${fmtDate(l.validade)}`}
                      {l.vencido && " · VENCIDO"}
                    </li>
                  ))}
                </ul>
              </td>
              <td className="py-1.5">
                <Badge variant={OPME_STATUS[o.status].variant} className="text-[11px]">
                  {OPME_STATUS[o.status].label}
                </Badge>
                {o.justificativa && <span className="block text-xs text-muted-foreground">Justificativa: {o.justificativa}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Conciliação de OPME no ditado, com a guia autorizada (manual, ClaimResponse ou TISS) e a leitura das etiquetas. */
export function OpmePanel({
  report,
  patientId,
  consultId,
  guide,
  onInsights,
}: {
  report: SurgicalReport;
  patientId: string;
  consultId?: number;
  guide?: OpmeGuideInfo | null;
  onInsights?: (insights: ConsultInsights | null, patch?: Partial<Consultation>) => void;
}) {
  const editable = consultId !== undefined && onInsights !== undefined;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <PackageCheck aria-hidden className="size-5 text-primary" /> Conciliação de OPME e materiais
        </CardTitle>
        <CardDescription>
          Narrado no ditado × autorizado na guia × consumido (etiqueta GS1). Catálogo e códigos de demonstração (não são TUSS).
          {guide && (
            <>
              {" "}
              Guia: {guide.origem === "manual" ? "entrada manual" : guide.origem === "tiss" ? "XML TISS" : "ClaimResponse"}
              {guide.numero && ` nº ${guide.numero}`}.
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <OpmeTable lines={report.opme} patientId={patientId} />
        {editable && (
          <>
            <GuideEditor lines={report.opme} consultId={consultId} onInsights={onInsights} />
            <LabelScanner consultId={consultId} lines={report.opme} onInsights={onInsights} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

type GuideItem = { codigo: string; descricao: string; quantidade: number };

type InsightsCallback = (insights: ConsultInsights | null, patch?: Partial<Consultation>) => void;

function GuideEditor({ lines, consultId, onInsights }: { lines: OpmeLine[]; consultId: number; onInsights: InsightsCallback }) {
  const [items, setItems] = useState<GuideItem[]>(() =>
    lines.filter((l) => l.autorizado > 0).map((l) => ({ codigo: l.codigo, descricao: l.descricao, quantidade: l.autorizado })),
  );
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<OpmeCatalogItem[]>([]);
  const [custom, setCustom] = useState({ codigo: "", descricao: "" });
  const [numero, setNumero] = useState("");
  const [paste, setPaste] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  useEffect(() => {
    if (query.trim().length < 2) {
      setFound([]);
      return;
    }
    const t = setTimeout(() => {
      api<OpmeCatalogItem[]>(`/consult/opme-catalog?q=${encodeURIComponent(query)}`)
        .then(setFound)
        .catch(() => setFound([]));
    }, 200);
    return () => clearTimeout(t);
  }, [query]);

  function add(item: GuideItem) {
    setItems((cur) =>
      cur.some((c) => c.codigo === item.codigo)
        ? cur.map((c) => (c.codigo === item.codigo ? { ...c, quantidade: c.quantidade + item.quantidade } : c))
        : [...cur, item],
    );
    setQuery("");
    setFound([]);
    setCustom({ codigo: "", descricao: "" });
  }

  async function send(body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      const res = await api<{ insights: ConsultInsights; autorizado: GuideItem[]; guia: OpmeGuideInfo }>(`/consultations/${consultId}/opme-autorizado`, {
        method: "PUT",
        json: { ...body, ...(numero.trim() ? { numero: numero.trim() } : {}) },
      });
      setItems(res.autorizado.map((a) => ({ codigo: a.codigo, descricao: a.descricao, quantidade: a.quantidade })));
      onInsights(res.insights, { opme_guia: res.guia });
      setOk(`Guia salva com ${res.autorizado.length} item(ns).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao salvar a guia");
    } finally {
      setBusy(false);
    }
  }

  async function importText(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (trimmed.startsWith("<")) return send({ tiss_xml: trimmed });
    try {
      await send({ claim_response: JSON.parse(trimmed) });
    } catch (e) {
      if (e instanceof SyntaxError) setError("O texto não é um JSON FHIR (ClaimResponse) nem um XML TISS.");
    }
  }

  async function onFile(file: File | undefined) {
    if (file) {
      const text = await file.text();
      setPaste(text);
      await importText(text);
    }
  }

  return (
    <details className="rounded-md border p-3">
      <summary className="min-h-9 cursor-pointer text-sm font-bold">Guia autorizada de OPME ({items.length} item(ns))</summary>
      <div className="mt-2 space-y-3">
        <div className="space-y-1">
          <Label htmlFor="opme-busca">Buscar material no catálogo</Label>
          <Input id="opme-busca" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="ex.: trocarte, componente femoral" autoComplete="off" />
          {found.length > 0 && (
            <ul aria-label="Resultados do catálogo" className="divide-y rounded-md border">
              {found.map((f) => (
                <li key={f.codigo}>
                  <button
                    type="button"
                    className="flex min-h-10 w-full items-center justify-between gap-2 px-2 text-left text-sm hover:bg-muted"
                    onClick={() => add({ codigo: f.codigo, descricao: f.descricao, quantidade: 1 })}
                  >
                    <span>{f.descricao}</span>
                    <span className="font-mono text-xs text-muted-foreground">{f.codigo}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {query.trim().length >= 2 && found.length === 0 && (
            <div className="grid gap-2 sm:grid-cols-[10rem_1fr_auto] sm:items-end">
              <div>
                <Label htmlFor="opme-cod">Código (fora do catálogo)</Label>
                <Input id="opme-cod" value={custom.codigo} onChange={(e) => setCustom({ ...custom, codigo: e.target.value })} />
              </div>
              <div>
                <Label htmlFor="opme-desc">Descrição</Label>
                <Input id="opme-desc" value={custom.descricao || query} onChange={(e) => setCustom({ ...custom, descricao: e.target.value })} />
              </div>
              <Button
                variant="outline"
                disabled={!custom.codigo.trim()}
                onClick={() => add({ codigo: custom.codigo.trim(), descricao: (custom.descricao || query).trim(), quantidade: 1 })}
              >
                <Plus aria-hidden /> Adicionar
              </Button>
            </div>
          )}
        </div>
        {items.length > 0 && (
          <ul className="space-y-1">
            {items.map((it) => (
              <li key={it.codigo} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="flex-1">
                  {it.descricao} <span className="font-mono text-xs text-muted-foreground">{it.codigo}</span>
                </span>
                <Input
                  type="number"
                  min={1}
                  max={99}
                  aria-label={`Quantidade autorizada de ${it.descricao}`}
                  className="w-20"
                  value={it.quantidade}
                  onChange={(e) =>
                    setItems((cur) => cur.map((c) => (c.codigo === it.codigo ? { ...c, quantidade: Math.max(1, Math.min(99, Number(e.target.value) || 1)) } : c)))
                  }
                />
                <Button variant="ghost" size="sm" aria-label={`Retirar ${it.descricao} da guia`} onClick={() => setItems((cur) => cur.filter((c) => c.codigo !== it.codigo))}>
                  <Trash2 aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
          <div className="space-y-1">
            <Label htmlFor="opme-guia-numero">Número da guia (opcional)</Label>
            <Input id="opme-guia-numero" value={numero} onChange={(e) => setNumero(e.target.value)} maxLength={60} />
          </div>
          <Button
            disabled={busy || items.length === 0}
            onClick={() => void send({ itens: items.map(({ codigo, descricao, quantidade }) => ({ codigo, descricao, quantidade })) })}
          >
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <PackageCheck aria-hidden />} Salvar guia
          </Button>
        </div>
        <div className="space-y-1 border-t pt-2">
          <Label htmlFor="opme-import">Importar ClaimResponse (JSON FHIR) ou guia TISS (XML)</Label>
          <Textarea id="opme-import" value={paste} onChange={(e) => setPaste(e.target.value)} className="min-h-16 font-mono text-xs" />
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={busy || !paste.trim()} onClick={() => void importText(paste)}>
              <FileUp aria-hidden /> Importar texto
            </Button>
            <Label htmlFor="opme-file" className="inline-flex min-h-11 cursor-pointer items-center gap-1 rounded-md border px-3 text-sm font-bold">
              <FileUp aria-hidden className="size-4" /> Escolher arquivo
            </Label>
            <input id="opme-file" type="file" accept=".json,.xml,application/json,text/xml" className="sr-only" onChange={(e) => void onFile(e.target.files?.[0])} />
          </div>
        </div>
        {error && <Alert variant="critico">{error}</Alert>}
        {ok && (
          <p role="status" className="text-sm font-bold text-ok">
            {ok}
          </p>
        )}
      </div>
    </details>
  );
}

function LabelScanner({ lines, consultId, onInsights }: { lines: OpmeLine[]; consultId: number; onInsights: InsightsCallback }) {
  const [label, setLabel] = useState("");
  const [quantity, setQuantity] = useState(1);
  const [busy, setBusy] = useState(false);
  const [camera, setCamera] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<OpmeScanResult["leitura"] | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const supported = typeof window !== "undefined" && "BarcodeDetector" in window && !!navigator.mediaDevices?.getUserMedia;
  const reads = lines.flatMap((l) => l.lotes.map((lote) => ({ ...lote, descricao: l.descricao })));

  async function submit(text: string) {
    const raw = text.trim();
    if (!raw) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api<OpmeScanResult>(`/consultations/${consultId}/opme-consumo`, { method: "POST", json: { etiqueta: raw, quantidade: quantity } });
      setLast(res.leitura);
      setLabel("");
      onInsights(res.insights);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao ler a etiqueta");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    try {
      const res = await api<{ insights: ConsultInsights }>(`/consultations/${consultId}/opme-consumo/${id}`, { method: "DELETE" });
      onInsights(res.insights);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao remover a leitura");
    } finally {
      setBusy(false);
    }
  }

  // Câmera do tablet: BarcodeDetector (DataMatrix e GS1-128). Sem suporte, vale o leitor USB (digita no campo) ou colar o texto.
  useEffect(() => {
    if (!camera) return;
    let stream: MediaStream | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;
    let stopped = false;
    (async () => {
      try {
        const Detector = (window as unknown as { BarcodeDetector: BarcodeDetectorCtor }).BarcodeDetector;
        const detector = new Detector({ formats: ["data_matrix", "code_128", "qr_code"] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
        if (stopped || !video.current) return;
        video.current.srcObject = stream;
        await video.current.play();
        timer = setInterval(async () => {
          if (!video.current) return;
          const codes = await detector.detect(video.current).catch(() => []);
          if (codes.length > 0) {
            setCamera(false);
            await submit(codes[0].rawValue);
          }
        }, 400);
      } catch {
        setError("Não foi possível abrir a câmera. Use um leitor USB ou digite/cole a etiqueta.");
        setCamera(false);
      }
    })();
    return () => {
      stopped = true;
      if (timer) clearInterval(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera]);

  return (
    <div className="space-y-2 rounded-md border p-3">
      <h3 className="flex items-center gap-2 text-sm font-bold">
        <ScanBarcode aria-hidden className="size-4" /> Etiqueta do implante (GS1)
      </h3>
      <form
        className="grid gap-2 sm:grid-cols-[1fr_5rem_auto_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          void submit(label);
        }}
      >
        <div className="space-y-1">
          <Label htmlFor="opme-etiqueta">Leitura da etiqueta (leitor USB, câmera ou texto)</Label>
          <Input
            id="opme-etiqueta"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="(01)07891234567895(17)271231(10)LOTE123(21)SN1"
            className="font-mono text-xs"
            autoComplete="off"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="opme-qtd">Qtd.</Label>
          <Input id="opme-qtd" type="number" min={1} max={99} value={quantity} onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))} />
        </div>
        <Button type="submit" disabled={busy || !label.trim()}>
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : <ScanBarcode aria-hidden />} Registrar
        </Button>
        {supported ? (
          <Button type="button" variant="outline" aria-pressed={camera} onClick={() => setCamera((c) => !c)}>
            {camera ? <CameraOff aria-hidden /> : <Camera aria-hidden />} {camera ? "Parar câmera" : "Ler com a câmera"}
          </Button>
        ) : (
          <span className="text-xs text-muted-foreground">Este navegador não lê códigos pela câmera: use um leitor USB.</span>
        )}
      </form>
      {camera && <video ref={video} aria-label="Imagem da câmera para ler a etiqueta" className="max-h-56 w-full rounded-md bg-black" playsInline muted />}
      {error && <Alert variant="critico">{error}</Alert>}
      {last && (
        <Alert variant={last.vencido ? "critico" : last.avisos.length ? "atencao" : "ok"}>
          <ScanBarcode aria-hidden />
          <span>
            Lido: <b>{last.descricao}</b> · lote {last.lote ?? "—"}
            {last.serie && ` · série ${last.serie}`}
            {last.validade && ` · validade ${fmtDate(last.validade)}`}
            {last.vencido && " — VALIDADE VENCIDA: não implantar este item."}
            {last.avisos.map((a) => (
              <span key={a} className="block">
                {a}
              </span>
            ))}
          </span>
        </Alert>
      )}
      {reads.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label="Etiquetas lidas">
          {reads.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-2">
              <span className="flex-1">
                {r.descricao} · lote {r.lote ?? "—"}
                {r.serie && ` · série ${r.serie}`}
                {r.validade && ` · val. ${fmtDate(r.validade)}`} {r.vencido && <Badge variant="critico">Vencido</Badge>}
              </span>
              <Button variant="ghost" size="sm" aria-label={`Remover a leitura do lote ${r.lote ?? r.id}`} disabled={busy} onClick={() => void remove(r.id)}>
                <Trash2 aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
