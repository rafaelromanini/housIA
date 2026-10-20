"use client";

import { useEffect, useState } from "react";
import { ClipboardCopy, Clock, FileText, IdCard, Languages, Loader2, Pill, Printer, ShieldAlert, TriangleAlert } from "lucide-react";

import { AiBadge, reidentify } from "@/components/ai/ai-badge";
import { FeedbackButtons } from "@/components/ai/feedback";
import { usePatient } from "@/components/patient/patient-context";
import { SourceRefs } from "@/components/source-ref";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type ConsultConfig, type DischargeDocuments, type DocSection, type ImplantCard } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const LANGS = [
  { value: "pt-BR", label: "Português" },
  { value: "es", label: "Español" },
  { value: "en", label: "English" },
] as const;

const SOAP_LABEL = { subjetivo: "S — Subjetivo", objetivo: "O — Objetivo", avaliacao: "A — Avaliação", plano: "P — Plano" } as const;

// Textos fixos da folha do paciente no mesmo idioma do conteúdo (sem tradução, o conteúdo fica em português).
const HANDOUT_LABELS = {
  "pt-BR": {
    meds: "Remédios",
    how: "Como usar",
    why: "Para que serve",
    alert: "Procure atendimento se tiver",
    care: "Cuidados em casa",
    followup: "Retorno",
    implant: "Cartão do implante",
    implantNote: "Guarde este cartão e mostre-o em consultas, exames de imagem (como ressonância) e em qualquer atendimento.",
    lot: "Lote",
    serial: "Série",
    valid: "Validade",
    registry: "Registro ANVISA",
    footer: "Documento gerado com apoio de IA e revisado pela equipe. Em caso de dúvida, ligue para a unidade ou procure a UBS mais próxima.",
    demo: "AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS.",
  },
  es: {
    meds: "Medicamentos",
    how: "Cómo usarlo",
    why: "Para qué sirve",
    alert: "Busque atención médica si tiene",
    care: "Cuidados en casa",
    followup: "Control",
    implant: "Tarjeta del implante",
    implantNote: "Guarde esta tarjeta y muéstrela en consultas, estudios de imagen (como resonancia) y en cualquier atención.",
    lot: "Lote",
    serial: "Serie",
    valid: "Vencimiento",
    registry: "Registro ANVISA",
    footer: "Documento elaborado con apoyo de IA y revisado por el equipo. Si tiene dudas, llame a la unidad o acuda a la UBS (unidad básica de salud) más cercana.",
    demo: "ENTORNO DE DEMOSTRACIÓN — DATOS SINTÉTICOS.",
  },
  en: {
    meds: "Medicines",
    how: "How to take",
    why: "What it is for",
    alert: "Get medical help if you have",
    care: "Care at home",
    followup: "Follow-up",
    implant: "Implant card",
    implantNote: "Keep this card and show it at appointments, imaging exams (such as MRI) and any care visit.",
    lot: "Lot",
    serial: "Serial",
    valid: "Expiry",
    registry: "ANVISA registration",
    footer: "Prepared with AI support and reviewed by the care team. If you have questions, call the unit or go to the nearest primary care clinic (UBS).",
    demo: "DEMO ENVIRONMENT — SYNTHETIC DATA.",
  },
} as const;

export function DischargeDocumentsCard() {
  const { patient } = usePatient();
  const [lang, setLang] = useState<DischargeDocuments["idioma"]>("pt-BR");
  // mesma lista de idiomas da consulta (≥ 95); quem não pode ler a configuração fica com os três de sempre
  const config = useApi<ConsultConfig>("/consult/config");
  const langOptions = config.data?.idiomas ?? LANGS;
  const [doc, setDoc] = useState<DischargeDocuments | null>(null);
  const [tab, setTab] = useState<"soap" | "orientacoes">("orientacoes");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api<DischargeDocuments | null>(`/patients/${patient.id}/discharge-documents/latest`)
      .then(setDoc)
      .catch(() => undefined);
  }, [patient.id]);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      setDoc(await api<DischargeDocuments>(`/patients/${patient.id}/discharge-documents`, { method: "POST", json: { idioma: lang } }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao gerar documentos");
    } finally {
      setBusy(false);
    }
  }

  const name = patient.nome;
  const r = (t: string) => reidentify(t, name);
  const L = HANDOUT_LABELS[(doc?.meta.traducao_disponivel ? doc.idioma : "pt-BR") as keyof typeof HANDOUT_LABELS] ?? HANDOUT_LABELS["pt-BR"];
  const implants = doc?.cartao_implante ?? [];
  const implantLine = (i: ImplantCard) =>
    [
      i.descricao,
      i.lote && `lote ${i.lote}`,
      i.serie && `série ${i.serie}`,
      i.validade && `validade ${i.validade.slice(0, 10).split("-").reverse().join("/")}`,
      i.registro_anvisa && `registro ANVISA ${i.registro_anvisa}`,
    ]
      .filter(Boolean)
      .join(", ");

  async function copySoap() {
    if (!doc) return;
    const text = [
      `SUMÁRIO DE ALTA — ${name}`,
      ...(Object.keys(SOAP_LABEL) as (keyof typeof SOAP_LABEL)[]).map((k) => `${SOAP_LABEL[k]}: ${r(doc.soap[k].texto)}`),
      ...(implants.length ? [`Implantes: ${implants.map(implantLine).join("; ")}`] : []),
      "Gerado por IA (HousIA) a partir do prontuário — revisar antes de registrar.",
    ].join("\n\n");
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function print() {
    document.body.classList.add("printing-handout");
    const done = () => {
      document.body.classList.remove("printing-handout");
      window.removeEventListener("afterprint", done);
    };
    window.addEventListener("afterprint", done);
    window.print();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileText aria-hidden className="size-5 text-primary" /> Documentos de alta
        </CardTitle>
        <CardDescription>
          Sumário SOAP para colar no prontuário e orientações em linguagem simples para o paciente/cuidador.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1.5">
            <Label htmlFor="idioma">Idioma das orientações</Label>
            <NativeSelect id="idioma" value={lang} onChange={(e) => setLang(e.target.value)}>
              {langOptions.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </NativeSelect>
          </div>
          <Button onClick={() => void generate()} disabled={busy}>
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Languages aria-hidden />}
            {busy ? "Gerando…" : doc ? "Gerar novamente" : "Gerar documentos"}
          </Button>
        </div>
        {error && (
          <Alert variant="critico">
            <ShieldAlert aria-hidden />
            {error}
          </Alert>
        )}
        {doc && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <AiBadge />
              {doc.meta.modo === "mock" && <Badge variant="outline">Modo demonstração (sem LLM)</Badge>}
              {!doc.meta.traducao_disponivel && <Badge variant="atencao">Tradução requer LLM real — exibido em português</Badge>}
              <span>
                {fmtDateTime(doc.created_at)} · {doc.model}
              </span>
            </div>
            <div role="tablist" aria-label="Documento" className="inline-flex rounded-md border bg-muted p-1">
              {(
                [
                  ["orientacoes", "Orientações ao paciente"],
                  ["soap", "Sumário SOAP (prontuário)"],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  role="tab"
                  type="button"
                  aria-selected={tab === key}
                  onClick={() => setTab(key)}
                  className={cn(
                    "min-h-10 rounded px-3 text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    tab === key ? "bg-card text-primary shadow-sm" : "text-muted-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>

            {tab === "soap" && (
              <div className="space-y-2">
                {(Object.keys(SOAP_LABEL) as (keyof typeof SOAP_LABEL)[]).map((k) => (
                  <SectionBlock key={k} title={SOAP_LABEL[k]} section={doc.soap[k]} patientId={patient.id} r={r} />
                ))}
                <Button variant="outline" size="sm" onClick={() => void copySoap()}>
                  <ClipboardCopy aria-hidden /> {copied ? "Copiado!" : "Copiar para o prontuário"}
                </Button>
              </div>
            )}

            {tab === "orientacoes" && (
              <div className="space-y-3">
                <div id="handout" className="space-y-4 rounded-lg border bg-card p-4 text-base leading-relaxed">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted-foreground">HousIA · {patient.nome}</p>
                    <h3 className="text-xl font-bold">{doc.orientacoes.titulo}</h3>
                    <p className="mt-1">{r(doc.orientacoes.resumo_internacao.texto)}</p>
                  </div>
                  {doc.orientacoes.medicamentos.length > 0 && (
                    <section aria-labelledby="handout-meds">
                      <h4 id="handout-meds" className="mb-1.5 flex items-center gap-1.5 font-bold">
                        <Pill aria-hidden className="size-4" /> {L.meds}
                      </h4>
                      <ul className="divide-y rounded-md border">
                        {doc.orientacoes.medicamentos.map((m, i) => (
                          <li key={i} className="space-y-0.5 p-3">
                            <div className="flex flex-wrap items-baseline justify-between gap-x-4">
                              <p className="text-lg font-bold">{m.medicamento}</p>
                              <p className="inline-flex items-center gap-1 font-bold text-primary">
                                <Clock aria-hidden className="size-4 self-center" /> {m.horarios.join(" · ")}
                              </p>
                            </div>
                            <p>
                              <span className="font-bold">{L.how}:</span> {r(m.como_usar)}
                            </p>
                            <p>
                              <span className="font-bold">{L.why}:</span> {r(m.para_que)}
                            </p>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}
                  <HandoutList title={L.alert} items={doc.orientacoes.sinais_de_alerta} r={r} alert />
                  <HandoutList title={L.care} items={doc.orientacoes.cuidados_em_casa} r={r} />
                  {implants.length > 0 && (
                    <section aria-labelledby="handout-implant" className="rounded-md border-2 border-primary/40 p-3">
                      <h4 id="handout-implant" className="mb-1 flex items-center gap-1.5 font-bold">
                        <IdCard aria-hidden className="size-4" /> {L.implant}
                      </h4>
                      <ul className="space-y-2">
                        {implants.map((i, k) => (
                          <li key={i.refs[0] ?? k} className="text-sm">
                            <p className="text-base font-bold">{i.descricao}</p>
                            <p>
                              {i.lote && (
                                <>
                                  <span className="font-bold">{L.lot}:</span> {i.lote}{" "}
                                </>
                              )}
                              {i.serie && (
                                <>
                                  · <span className="font-bold">{L.serial}:</span> {i.serie}{" "}
                                </>
                              )}
                              {i.validade && (
                                <>
                                  · <span className="font-bold">{L.valid}:</span> {i.validade.slice(0, 10).split("-").reverse().join("/")}{" "}
                                </>
                              )}
                              {i.registro_anvisa && (
                                <>
                                  · <span className="font-bold">{L.registry}:</span> {i.registro_anvisa}
                                </>
                              )}
                            </p>
                            {(i.procedimento || i.colocado_em) && (
                              <p className="text-muted-foreground">
                                {i.procedimento}
                                {i.colocado_em && ` — ${fmtDateTime(i.colocado_em)}`}
                              </p>
                            )}
                          </li>
                        ))}
                      </ul>
                      <p className="mt-1 text-xs text-muted-foreground">{L.implantNote}</p>
                    </section>
                  )}
                  <div>
                    <p className="font-bold">{L.followup}</p>
                    <p>{r(doc.orientacoes.retorno.texto)}</p>
                  </div>
                  <p className="border-t pt-2 text-xs text-muted-foreground">
                    {L.footer} {L.demo}
                  </p>
                </div>
                <details className="text-xs">
                  <summary className="cursor-pointer font-bold text-muted-foreground">Fontes de cada orientação (para a equipe)</summary>
                  <ul className="mt-2 space-y-1">
                    {[
                      ...doc.orientacoes.medicamentos.map((m) => ({ label: m.medicamento, refs: m.refs })),
                      ...doc.orientacoes.sinais_de_alerta.map((s) => ({ label: s.texto, refs: s.refs })),
                      ...doc.orientacoes.cuidados_em_casa.map((s) => ({ label: s.texto, refs: s.refs })),
                      ...implants.map((i) => ({ label: `${L.implant}: ${implantLine(i)}`, refs: i.refs })),
                    ].map((x, i) => (
                      <li key={i} className="flex flex-wrap items-center gap-1">
                        <span className="line-clamp-1 max-w-md">{r(x.label)}</span>
                        <SourceRefs patientId={patient.id} refs={x.refs} label="fonte" />
                      </li>
                    ))}
                  </ul>
                </details>
                <Button variant="outline" size="sm" onClick={print}>
                  <Printer aria-hidden /> Imprimir orientações
                </Button>
              </div>
            )}
            <FeedbackButtons kind="discharge_docs" entityId={doc.id} patientId={patient.id} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

function SectionBlock({ title, section, patientId, r }: { title: string; section: DocSection; patientId: string; r: (t: string) => string }) {
  return (
    <div className="rounded-md border p-3">
      <p className="text-sm font-bold">{title}</p>
      <p className="text-sm">{r(section.texto)}</p>
      {section.sem_fonte ? (
        <p className="mt-1 inline-flex items-center gap-1 text-xs font-bold text-atencao">
          <TriangleAlert aria-hidden className="size-3.5" /> Sem fonte verificável — revisar
        </p>
      ) : (
        <SourceRefs patientId={patientId} refs={section.refs} />
      )}
    </div>
  );
}

function HandoutList({ title, items, r, alert }: { title: string; items: DocSection[]; r: (t: string) => string; alert?: boolean }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className={cn("font-bold", alert && "text-critico")}>{title}</p>
      <ul className="list-disc space-y-1 pl-5">
        {items.map((s, i) => (
          <li key={i}>{r(s.texto)}</li>
        ))}
      </ul>
    </div>
  );
}
