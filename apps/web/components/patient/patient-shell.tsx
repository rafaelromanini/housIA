"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ArrowLeft, BedDouble, CalendarDays, ClipboardCheck, House, LayoutList, Mic, OctagonAlert, Pill, Sparkles, Stethoscope } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { PatientContext } from "@/components/patient/patient-context";
import { AlertCountBadges, RiskBadge } from "@/components/severity";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { PatientDetail } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { cn } from "@/lib/utils";

export function PatientShell({ patientId, children }: { patientId: string; children: React.ReactNode }) {
  return (
    <AppShell>
      <PatientLoader patientId={patientId}>{children}</PatientLoader>
    </AppShell>
  );
}

function PatientLoader({ patientId, children }: { patientId: string; children: React.ReactNode }) {
  const { data, error, loading, reload } = useApi<PatientDetail>(`/patients/${encodeURIComponent(patientId)}`);

  if (error) return <Alert variant="critico">Não foi possível carregar o paciente: {error}</Alert>;
  if (loading && !data) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-11 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }
  if (!data) return null;

  return (
    <PatientContext.Provider value={{ patient: data, reload }}>
      <div className="space-y-4">
        <PatientHeader patient={data} />
        <PatientTabs patientId={data.id} inpatient={data.internado} />
        <div>{children}</div>
      </div>
    </PatientContext.Provider>
  );
}

function PatientHeader({ patient: p }: { patient: PatientDetail }) {
  const allergies = p.contexto.alergias;
  return (
    <section aria-label="Identificação do paciente" className="rounded-lg border bg-card p-4 shadow-sm space-y-3">
      <Link
        href={p.internado ? "/leitos" : "/consultas"}
        className="inline-flex min-h-9 items-center gap-1 text-sm font-bold text-primary hover:underline"
      >
        <ArrowLeft aria-hidden className="size-4" /> {p.internado ? "Painel de leitos" : "Agenda de consultas"}
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold leading-tight">{p.nome}</h1>
          <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
            <span>
              {p.idade ?? "—"} anos · {p.sexo}
            </span>
            {p.internado ? (
              <>
                <span className="inline-flex items-center gap-1">
                  <BedDouble aria-hidden className="size-4" />
                  {p.setor} · Leito {p.leito}
                </span>
                <span className="inline-flex items-center gap-1">
                  <CalendarDays aria-hidden className="size-4" />
                  {p.dias_internacao} dia(s) · internação {p.tipo_internacao}
                  {p.internado_em && ` desde ${fmtDateTime(p.internado_em)}`}
                </span>
              </>
            ) : (
              p.ambulatorial && (
                <span className="inline-flex items-center gap-1">
                  <Stethoscope aria-hidden className="size-4" />
                  {p.ambulatorial.servico}
                  {p.ambulatorial.inicio && ` · chegada ${fmtDateTime(p.ambulatorial.inicio)}`}
                </span>
              )
            )}
          </p>
          <p className="text-sm">{p.internado ? p.motivo : p.ambulatorial?.motivo}</p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <AlertCountBadges counts={p.alertas_abertos} />
          {p.risco_alta && <RiskBadge risk={p.risco_alta} prefix="Risco de alta" />}
        </div>
      </div>
      {allergies.length > 0 ? (
        <Alert variant="critico">
          <OctagonAlert aria-hidden />
          <div>
            <p className="font-bold">Alergias</p>
            <ul className="list-disc pl-4">
              {allergies.map((a) => (
                <li key={a.source_ref}>{a.summary}</li>
              ))}
            </ul>
          </div>
        </Alert>
      ) : (
        <p className="text-sm">
          <Badge variant="outline">{p.contexto.nega_alergias ? "Nega alergias conhecidas" : "Sem registro de alergias"}</Badge>
        </p>
      )}
      {p.cenario && (
        <details className="rounded-md border border-dashed border-primary/50 bg-secondary/60 p-3 text-sm">
          <summary className="flex min-h-9 items-center gap-2 font-bold">
            <Sparkles aria-hidden className="size-4 text-primary" />
            {p.cenario.titulo}
          </summary>
          <p className="mt-2">
            <span className="font-bold">O que esperar na demo: </span>
            {p.cenario.esperado}
          </p>
        </details>
      )}
    </section>
  );
}

const TABS = [
  { href: "", label: "Visão geral", Icon: LayoutList, inpatientOnly: false },
  { href: "/consulta", label: "Consulta com IA", Icon: Mic, inpatientOnly: false },
  { href: "/passagem", label: "Passagem de plantão", Icon: ClipboardCheck, inpatientOnly: true },
  { href: "/alta", label: "Planejar alta", Icon: House, inpatientOnly: true },
  { href: "/reconciliacao", label: "Reconciliação", Icon: Pill, inpatientOnly: true },
];

function PatientTabs({ patientId, inpatient }: { patientId: string; inpatient: boolean }) {
  const pathname = usePathname();
  const base = `/pacientes/${patientId}`;
  return (
    <nav aria-label="Seções do paciente" className="overflow-x-auto">
      <ul className="flex min-w-max gap-1 border-b">
        {TABS.filter((t) => inpatient || !t.inpatientOnly).map(({ href, label, Icon }) => {
          const target = `${base}${href}`;
          const active = href === "" ? pathname === base : pathname.startsWith(target);
          return (
            <li key={href}>
              <Link
                href={target}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "-mb-px inline-flex min-h-11 items-center gap-2 border-b-2 px-3 text-sm font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  active ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon aria-hidden className="size-4" />
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
