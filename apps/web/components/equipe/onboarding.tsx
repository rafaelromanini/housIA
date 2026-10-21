"use client";

import { CheckCircle2, Circle } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { OnboardingConsultorio } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { useAuth } from "@/lib/auth";

/** Onboarding do consultório em 3 passos: some quando a primeira nota é aprovada no sandbox. */
export function Onboarding() {
  const { user } = useAuth();
  const consultorio = user?.organizacao?.tipo === "consultorio";
  const { data } = useApi<OnboardingConsultorio>(consultorio ? "/consult/onboarding" : null);
  if (!data || data.concluido) return null;
  const feitos = data.passos.filter((p) => p.feito).length;
  return (
    <Card className="mb-4" data-testid="onboarding">
      <CardHeader>
        <CardTitle className="text-base">
          Primeiros passos ({feitos} de {data.passos.length})
        </CardTitle>
        <CardDescription>
          Teste sem gastar o teste grátis: na aba Sandbox, escolha um cenário sintético. O onboarding termina na sua primeira nota aprovada.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="space-y-1.5">
          {data.passos.map((p) => (
            <li key={p.chave} className="flex items-center gap-2 text-sm">
              {p.feito ? (
                <CheckCircle2 aria-hidden className="size-5 text-ok" />
              ) : (
                <Circle aria-hidden className="size-5 text-muted-foreground" />
              )}
              <span className={p.feito ? "text-muted-foreground line-through" : "font-bold"}>{p.titulo}</span>
              <span className="sr-only">{p.feito ? " (feito)" : " (pendente)"}</span>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}
