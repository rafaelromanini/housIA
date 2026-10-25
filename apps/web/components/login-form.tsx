"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { LogIn, Stethoscope } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/lib/auth";

const DEMO_USERS = [
  { email: "medico@housia.dev", label: "Médica" },
  { email: "enfermeira@housia.dev", label: "Enfermagem" },
  { email: "gestor@housia.dev", label: "Gestão" },
];

export function LoginForm() {
  const { login } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("medico@housia.dev");
  const [password, setPassword] = useState("housia123");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const u = await login(email, password);
      const next = params.get("next");
      // sem destino pedido: consultório vai para /consultas, hospital para /leitos
      router.push(next && next.startsWith("/") && !next.startsWith("//") ? next : u.destino);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha no login");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader>
        <div className="flex items-center gap-2 text-primary">
          <Stethoscope aria-hidden className="size-7" />
          <span className="text-2xl font-bold">HousIA</span>
        </div>
        <CardTitle className="text-lg">Entrar</CardTitle>
        <CardDescription>Consulta com IA com consentimento, transcrição ao vivo e nota SOAP revisada por você, além de passagem de plantão e alta com segurança.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="email">E-mail</Label>
            <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">Senha</Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "login-error" : undefined}
            />
          </div>
          {error && (
            <Alert variant="critico" id="login-error">
              {error}
            </Alert>
          )}
          <Button type="submit" className="w-full" disabled={busy}>
            <LogIn aria-hidden />
            {busy ? "Entrando…" : "Entrar"}
          </Button>
        </form>
        <div className="mt-4 flex flex-wrap justify-between gap-2 text-sm">
          <Link href="/esqueci" className="font-bold text-primary underline underline-offset-2 hover:no-underline">
            Esqueci a senha
          </Link>
          <Link href="/cadastro" className="font-bold text-primary underline underline-offset-2 hover:no-underline">
            Criar conta grátis
          </Link>
        </div>
        <div className="mt-5 border-t pt-4">
          <p className="text-sm font-bold mb-2">Usuários de demonstração (senha: housia123)</p>
          <div className="flex flex-wrap gap-2">
            {DEMO_USERS.map((u) => (
              <Button key={u.email} type="button" variant="outline" size="sm" onClick={() => setEmail(u.email)}>
                {u.label}
              </Button>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
