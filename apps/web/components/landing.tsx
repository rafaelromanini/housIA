"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { ArrowRight, Building2, ClipboardCheck, ShieldCheck, Stethoscope } from "lucide-react";

import { buttonVariants } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const DESTAQUES = [
  {
    Icon: Stethoscope,
    titulo: "Consulta com IA",
    texto: "Transcrição, alertas de segurança e nota pronta para revisar, sempre com a fonte de cada item.",
  },
  {
    Icon: ClipboardCheck,
    titulo: "Transições de cuidado",
    texto: "Passagem de plantão, reconciliação medicamentosa e preparo da alta em um só lugar.",
  },
  {
    Icon: ShieldCheck,
    titulo: "Profissional verificado",
    texto: "O registro no conselho é conferido e a nota exportada leva o selo de verificação.",
  },
];

export function Landing() {
  const { user, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && user) router.replace(user.destino); // quem já entrou vai direto ao seu painel
  }, [ready, user, router]);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-10 px-4 py-10">
      <header className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-2xl font-bold text-primary">
          <Stethoscope aria-hidden className="size-7" />
          HousIA
        </span>
        <Link href="/login" className={cn(buttonVariants({ variant: "outline" }))}>
          Entrar
        </Link>
      </header>
      <section className="space-y-4">
        <h1 className="text-3xl font-bold leading-tight sm:text-4xl">
          Menos tempo documentando, mais segurança em cada transição de cuidado.
        </h1>
        <p className="max-w-2xl text-lg text-muted-foreground">
          O HousIA ouve a consulta, aponta o que merece atenção e entrega a nota para o profissional revisar. Funciona
          em consultório e em hospital.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link href="/cadastro" className={cn(buttonVariants({ size: "lg" }))}>
            Começar grátis
            <ArrowRight aria-hidden />
          </Link>
          <Link href="/piloto" className={cn(buttonVariants({ size: "lg", variant: "outline" }))}>
            <Building2 aria-hidden />
            Piloto para hospital
          </Link>
        </div>
      </section>
      <section aria-label="Destaques" className="grid gap-4 sm:grid-cols-3">
        {DESTAQUES.map(({ Icon, titulo, texto }) => (
          <div key={titulo} className="rounded-lg border bg-card p-4 space-y-2">
            <Icon aria-hidden className="size-6 text-primary" />
            <h2 className="font-bold">{titulo}</h2>
            <p className="text-sm text-muted-foreground">{texto}</p>
          </div>
        ))}
      </section>
    </main>
  );
}
