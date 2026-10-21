"use client";

import Link from "next/link";
import { useState } from "react";
import { CalendarClock, ChevronRight, ClipboardList, FlaskConical, Search, Sparkles } from "lucide-react";

import { Agenda } from "@/components/consult/agenda";
import { NovoPaciente } from "@/components/consult/novo-paciente";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { CenarioSandbox, ConsultaBusca, ConsultaLinha, PlanoUso } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

type Aba = "agenda" | "todas" | "revisao" | "sandbox";

const STATUS = {
  em_andamento: { label: "Em andamento", variant: "atencao" },
  finalizada: { label: "Nota para revisar", variant: "info" },
  enviada: { label: "No prontuário", variant: "ok" },
} as const;

/** Consultas do consultório: agenda, busca global, fila de notas para revisar e sandbox. */
export function ConsultasView() {
  const [aba, setAba] = useState<Aba>("agenda");
  const plano = useApi<PlanoUso>("/consult/plano");
  const fila = useApi<ConsultaBusca>("/consultations?status=aguardando_revisao&limite=1");
  const pendentes = fila.data?.total ?? 0;
  const abas: { chave: Aba; rotulo: string; Icon: typeof Search }[] = [
    { chave: "agenda", rotulo: "Agenda", Icon: CalendarClock },
    { chave: "todas", rotulo: "Todas as consultas", Icon: Search },
    { chave: "revisao", rotulo: `Aguardando revisão${pendentes ? ` (${pendentes})` : ""}`, Icon: ClipboardList },
    { chave: "sandbox", rotulo: "Sandbox", Icon: FlaskConical },
  ];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PlanoResumo plano={plano.data} />
        <NovoPaciente />
      </div>
      <div role="tablist" aria-label="Consultas" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
        {abas.map(({ chave, rotulo, Icon }) => (
          <button
            key={chave}
            role="tab"
            id={`aba-${chave}`}
            aria-selected={aba === chave}
            aria-controls={`painel-${chave}`}
            onClick={() => setAba(chave)}
            className={cn(
              "inline-flex min-h-10 flex-1 items-center justify-center gap-1.5 rounded-md px-3 text-sm font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              aba === chave ? "bg-card text-primary shadow-sm" : "text-muted-foreground",
            )}
          >
            <Icon aria-hidden className="size-4" /> {rotulo}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`painel-${aba}`} aria-labelledby={`aba-${aba}`}>
        {aba === "agenda" && <Agenda />}
        {aba === "todas" && <Busca key="todas" />}
        {aba === "revisao" && <Busca key="revisao" fila onChange={() => void fila.reload()} />}
        {aba === "sandbox" && <Sandbox />}
      </div>
    </div>
  );
}

function PlanoResumo({ plano }: { plano: PlanoUso | null }) {
  if (!plano) return <Skeleton className="h-6 w-64" />;
  return (
    <div className="space-y-1">
      <p className="text-sm text-muted-foreground" data-testid="plano-uso">
        {plano.limite === null ? (
          <>
            <strong className="text-foreground">Plano {plano.nome}:</strong> sem limite de consultas.
          </>
        ) : (
          <>
            <strong className="text-foreground">
              {plano.usadas} de {plano.limite} consultas grátis
            </strong>{" "}
            usadas ({plano.restantes} restantes).
          </>
        )}{" "}
        O sandbox não conta ({plano.sandbox} {plano.sandbox === 1 ? "consulta" : "consultas"} de teste).
      </p>
      {plano.aviso && (
        <p role="status" className="text-sm font-bold text-amber-700" data-testid="plano-aviso">
          {plano.aviso}
        </p>
      )}
      {plano.vencido && (
        <p role="alert" className="text-sm font-bold text-destructive">
          O plano venceu. Peça a renovação para abrir novas consultas.
        </p>
      )}
    </div>
  );
}

function Busca({ fila = false, onChange }: { fila?: boolean; onChange?: () => void }) {
  const [form, setForm] = useState({ q: "", cid: "", status: "", de: "", ate: "", sandbox: "" });
  const [aplicado, setAplicado] = useState(form);
  const params = new URLSearchParams({ limite: "50" });
  if (fila) params.set("status", "aguardando_revisao");
  else if (aplicado.status) params.set("status", aplicado.status);
  for (const k of ["q", "cid", "de", "ate", "sandbox"] as const) if (aplicado[k]) params.set(k, aplicado[k]);
  const { data, error, loading } = useApi<ConsultaBusca>(`/consultations?${params}`);
  return (
    <div className="space-y-3">
      <form
        role="search"
        aria-label={fila ? "Filtrar a fila de revisão" : "Buscar consultas"}
        className="grid gap-3 rounded-lg border bg-card p-3 sm:grid-cols-2 lg:grid-cols-6"
        onSubmit={(e) => {
          e.preventDefault();
          setAplicado(form);
          onChange?.();
        }}
      >
        <div className="space-y-1.5 lg:col-span-2">
          <Label htmlFor={`q-${fila}`}>Paciente ou palavra</Label>
          <Input id={`q-${fila}`} value={form.q} onChange={(e) => setForm({ ...form, q: e.target.value })} maxLength={80} placeholder="Nome, especialidade…" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`cid-${fila}`}>CID-10</Label>
          <Input id={`cid-${fila}`} value={form.cid} onChange={(e) => setForm({ ...form, cid: e.target.value })} maxLength={12} placeholder="E11" />
        </div>
        {!fila && (
          <div className="space-y-1.5">
            <Label htmlFor="status-busca">Situação</Label>
            <NativeSelect id="status-busca" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
              <option value="">Todas</option>
              <option value="em_andamento">Em andamento</option>
              <option value="finalizada">Finalizada</option>
              <option value="enviada">No prontuário</option>
              <option value="aguardando_revisao">Aguardando revisão</option>
            </NativeSelect>
          </div>
        )}
        <div className="space-y-1.5">
          <Label htmlFor={`de-${fila}`}>De</Label>
          <Input id={`de-${fila}`} type="date" value={form.de} onChange={(e) => setForm({ ...form, de: e.target.value })} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`ate-${fila}`}>Até</Label>
          <Input id={`ate-${fila}`} type="date" value={form.ate} onChange={(e) => setForm({ ...form, ate: e.target.value })} />
        </div>
        <div className="flex items-end sm:col-span-2 lg:col-span-6">
          <Button type="submit">
            <Search aria-hidden /> Buscar
          </Button>
        </div>
      </form>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-40 w-full" />}
      {data && (
        <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
          {loading ? "Buscando…" : `${data.total} ${data.total === 1 ? "consulta" : "consultas"}`}
          {data.total > data.itens.length && ` (mostrando as ${data.itens.length} mais recentes)`}
        </p>
      )}
      {data && data.itens.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {fila ? "Nenhuma nota aguardando revisão. Tudo em dia." : "Nenhuma consulta encontrada com esses filtros."}
        </p>
      )}
      {data && data.itens.length > 0 && (
        <ul className="space-y-2">
          {data.itens.map((c) => (
            <Linha key={c.id} c={c} />
          ))}
        </ul>
      )}
    </div>
  );
}

function Linha({ c }: { c: ConsultaLinha }) {
  const st = STATUS[c.status];
  return (
    <li>
      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-4">
          <div className="min-w-48 flex-1">
            <p className="flex flex-wrap items-center gap-2 font-bold">
              {c.paciente}
              {c.sandbox && (
                <Badge variant="ai">
                  <FlaskConical aria-hidden /> sandbox
                </Badge>
              )}
            </p>
            <p className="text-sm text-muted-foreground">
              {fmtDateTime(c.created_at)} · {c.especialidade_rotulo} · {c.falas} falas
              {c.criado_por && ` · ${c.criado_por}`}
            </p>
            {c.cids.length > 0 && (
              <p className="mt-1 flex flex-wrap gap-1">
                {c.cids.slice(0, 4).map((x) => (
                  <Badge key={x.codigo} variant="outline" title={x.descricao}>
                    {x.codigo}
                  </Badge>
                ))}
              </p>
            )}
          </div>
          <Badge variant={st.variant}>{st.label}</Badge>
          <Button asChild variant={c.aguardando_revisao ? "default" : "outline"}>
            <Link href={`/pacientes/${encodeURIComponent(c.patient_id)}/consulta?consulta=${c.id}`}>
              {c.aguardando_revisao ? "Revisar nota" : "Abrir"} <ChevronRight aria-hidden />
            </Link>
          </Button>
        </CardContent>
      </Card>
    </li>
  );
}

function Sandbox() {
  const { data, error } = useApi<CenarioSandbox[]>("/consult/sandbox/cenarios");
  return (
    <div className="space-y-3">
      <Alert variant="info">
        <Sparkles aria-hidden /> O sandbox usa pacientes e conversas sintéticos para você testar à vontade. Não entra na contagem do teste
        grátis e a nota nunca é enviada ao prontuário.
      </Alert>
      {error && <Alert variant="critico">{error}</Alert>}
      {!data && !error && <Skeleton className="h-40 w-full" />}
      {data && (
        <ul className="space-y-2">
          {data.map((c) => (
            <li key={c.id}>
              <Card>
                <CardContent className="flex flex-wrap items-center gap-3 pt-4">
                  <div className="min-w-48 flex-1">
                    <p className="font-bold">{c.titulo}</p>
                    <p className="text-sm text-muted-foreground">
                      {c.paciente} · {c.especialidade_rotulo} · {c.tipo}
                    </p>
                  </div>
                  <Badge variant="ai">sintético</Badge>
                  <Button asChild>
                    <Link href={`/pacientes/${encodeURIComponent(c.patient_id)}/consulta?sandbox=1`}>
                      <FlaskConical aria-hidden /> Testar no sandbox
                    </Link>
                  </Button>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
