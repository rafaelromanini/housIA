"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import {
  BedDouble,
  CalendarClock,
  ChartNoAxesCombined,
  ClipboardList,
  Scale,
  LogOut,
  Mic,
  Route,
  Server,
  ShieldCheck,
  Stethoscope,
  Users,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/lib/auth";
import { ROLE_LABEL } from "@/lib/format";
import { cn } from "@/lib/utils";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, ready, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (ready && !user) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [ready, user, router, pathname]);

  if (!ready || !user) {
    return (
      <div className="mx-auto w-full max-w-7xl p-4 space-y-3">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  // Leitos concentra os módulos hospitalares (passagem de plantão, reconciliação, alta): sem nenhum no plano, some do menu
  const temModuloHospitalar = (user.plano?.modulos.length ?? 0) > 0;
  const nav = [
    { href: "/leitos", label: "Leitos", Icon: BedDouble, show: temModuloHospitalar },
    { href: "/consultas", label: "Consultas", Icon: CalendarClock, show: user.role !== "gestor" },
    { href: "/perfil-voz", label: "Minha voz", Icon: Mic, show: user.role !== "gestor" },
    { href: "/jornada", label: "Jornada", Icon: Route, show: user.role !== "medico" },
    { href: "/gestao", label: "Gestão", Icon: ClipboardList, show: user.role === "gestor" },
    { href: "/impacto", label: "Impacto", Icon: ChartNoAxesCombined, show: user.role === "gestor" },
    { href: "/auditoria", label: "Auditoria", Icon: ShieldCheck, show: user.role === "gestor" },
    { href: "/conformidade", label: "Conformidade", Icon: Scale, show: user.role === "gestor" },
    { href: "/equipe", label: "Equipe", Icon: Users, show: user.role === "gestor" },
    { href: "/ambiente", label: "Ambiente", Icon: Server, show: user.role === "gestor" },
  ].filter((n) => n.show);

  return (
    <>
      <header className="sticky top-0 z-40 border-b bg-card/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-3 px-4 py-2">
          <Link href={user.destino} className="flex items-center gap-2 font-bold text-primary text-lg mr-2 min-h-11">
            <Stethoscope aria-hidden className="size-6" />
            HousIA
          </Link>
          <nav aria-label="Principal" className="flex items-center gap-1">
            {nav.map(({ href, label, Icon }) => {
              const active = pathname.startsWith(href) || (href === "/leitos" && pathname.startsWith("/pacientes"));
              return (
                <Link
                  key={href}
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "inline-flex min-h-11 items-center gap-2 rounded-md px-3 text-sm font-bold transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    active && "bg-secondary text-secondary-foreground",
                  )}
                >
                  <Icon aria-hidden className="size-4" />
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <div className="hidden sm:flex flex-col items-end leading-tight">
              <span className="text-sm font-bold">{user.name}</span>
              <span className="text-xs text-muted-foreground">
                {ROLE_LABEL[user.role]}
                {user.organizacao ? ` · ${user.organizacao.nome}` : ""}
              </span>
            </div>
            <Button variant="ghost" size="icon" onClick={logout} aria-label="Sair">
              <LogOut aria-hidden />
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-4">{children}</main>
    </>
  );
}
