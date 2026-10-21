"use client";

import { CheckCircle2, Circle } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/lib/use-api";
import { useAuth } from "@/lib/auth";

interface Passo {
  chave: string;
  titulo: string;
  feito: boolean;
}

/** Onboarding do consultório: some quando a primeira nota é aprovada. */
export function Onboarding() {
  const { user } = useAuth();
  const orgId = user?.organizacao?.tipo === "consultorio" ? user.organizacao.id : undefined;
  const { data } = useApi<{ passos: Passo[]; concluido: boolean }>(orgId ? `/orgs/${orgId}/onboarding` : null);
  if (!data || data.concluido) return null;
  const feitos = data.passos.filter((p) => p.feito).length;
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="text-base">
          Primeiros passos ({feitos} de {data.passos.length})
        </CardTitle>
        <CardDescription>Falta pouco: o onboarding termina na sua primeira nota aprovada.</CardDescription>
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
