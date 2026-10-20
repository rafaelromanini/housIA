"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { BedDouble, CalendarDays, RefreshCw, Search, Sparkles } from "lucide-react";

import { AlertCountBadges, RiskBadge } from "@/components/severity";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { BedRow } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

const WARD_ORDER = ["UTI Adulto", "Enfermaria 3B", "Clínica Cirúrgica 4A", "Pronto-Socorro"];

export function BedPanel() {
  const { data, error, loading, reload } = useApi<BedRow[]>("/patients");
  const [ward, setWard] = useState("todos");
  const [query, setQuery] = useState("");
  const [onlyDemo, setOnlyDemo] = useState(false);

  const wards = useMemo(() => {
    const set = new Set((data ?? []).map((r) => r.setor));
    const rank = (w: string) => (WARD_ORDER.includes(w) ? WARD_ORDER.indexOf(w) : WARD_ORDER.length);
    return [...set].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  }, [data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (data ?? []).filter(
      (r) =>
        (ward === "todos" || r.setor === ward) &&
        (!onlyDemo || r.cenario) &&
        (!q || r.nome.toLowerCase().includes(q) || r.leito.toLowerCase().includes(q)),
    );
  }, [data, ward, query, onlyDemo]);

  const grouped = wards
    .map((w) => ({ ward: w, rows: filtered.filter((r) => r.setor === w) }))
    .filter((g) => g.rows.length > 0);

  const critical = (data ?? []).reduce((n, r) => n + r.alertas_abertos.critico, 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Painel de leitos</h1>
          <p className="text-sm text-muted-foreground">
            {data ? `${data.length} pacientes internados` : "Carregando…"}
            {critical > 0 && ` · ${critical} alerta(s) crítico(s) aberto(s)`}
          </p>
        </div>
        <Button variant="outline" onClick={() => void reload()} disabled={loading}>
          <RefreshCw aria-hidden className={cn(loading && "animate-spin")} />
          Atualizar
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-[minmax(0,240px)_minmax(0,1fr)_auto] items-end">
        <div className="space-y-1.5">
          <Label htmlFor="filtro-setor">Setor</Label>
          <NativeSelect id="filtro-setor" value={ward} onChange={(e) => setWard(e.target.value)}>
            <option value="todos">Todos os setores</option>
            {wards.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="busca">Buscar paciente ou leito</Label>
          <div className="relative">
            <Search aria-hidden className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input id="busca" className="pl-9" placeholder="Ex.: Antônio, 12" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
        </div>
        <Button variant={onlyDemo ? "default" : "outline"} aria-pressed={onlyDemo} onClick={() => setOnlyDemo((v) => !v)}>
          <Sparkles aria-hidden />
          Só cenários da demo
        </Button>
      </div>

      {error && <Alert variant="critico">Não foi possível carregar os leitos: {error}</Alert>}

      {loading && !data && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      )}

      {grouped.map(({ ward: w, rows }) => (
        <section key={w} aria-labelledby={`setor-${w}`} className="space-y-2">
          <h2 id={`setor-${w}`} className="flex items-center gap-2 text-lg font-bold">
            {w}
            <span className="text-sm font-normal text-muted-foreground">({rows.length})</span>
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {rows.map((r) => (
              <li key={r.id}>
                <BedCard row={r} />
              </li>
            ))}
          </ul>
        </section>
      ))}

      {data && grouped.length === 0 && <p className="text-muted-foreground">Nenhum paciente encontrado com esses filtros.</p>}
    </div>
  );
}

function BedCard({ row }: { row: BedRow }) {
  const critical = row.alertas_abertos.critico > 0;
  return (
    <Link
      href={`/pacientes/${row.id}`}
      className={cn(
        "flex h-full flex-col gap-2 rounded-lg border bg-card p-3 shadow-sm transition-[border-color,box-shadow] duration-150 hover:border-primary hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        critical && "border-l-4 border-l-critico",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm font-bold text-primary">
          <BedDouble aria-hidden className="size-4" />
          Leito {row.leito}
        </span>
        {row.risco_alta && <RiskBadge risk={row.risco_alta} prefix="Alta" />}
      </div>
      <div>
        <p className="font-bold leading-snug">{row.nome}</p>
        <p className="text-sm text-muted-foreground">
          {row.idade ?? "—"} anos · {row.sexo}
        </p>
      </div>
      <p className="line-clamp-2 text-sm">{row.motivo}</p>
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <CalendarDays aria-hidden className="size-3.5" />
          {row.dias_internacao === 0 ? "Internado hoje" : `${row.dias_internacao} dia(s) de internação`}
        </span>
      </div>
      <AlertCountBadges counts={row.alertas_abertos} />
      {row.cenario && (
        <Badge variant="ai" className="w-fit whitespace-normal">
          <Sparkles aria-hidden />
          {row.cenario.split(" — ")[0]}
        </Badge>
      )}
    </Link>
  );
}
