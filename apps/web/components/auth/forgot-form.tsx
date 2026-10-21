"use client";

import Link from "next/link";
import { useState } from "react";
import { Mail } from "lucide-react";

import { AuthCard, linkClass } from "@/components/auth/auth-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";

export function ForgotForm() {
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    try {
      const r = await api<{ detail: string }>("/auth/forgot", { method: "POST", json: { email } });
      setMsg(r.detail);
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível enviar o link");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard
      titulo="Esqueci a senha"
      descricao="Informe o e-mail da conta e enviaremos um link para criar uma nova senha."
      rodape={
        <Link href="/login" className={linkClass}>
          Voltar ao login
        </Link>
      }
    >
      {msg ? (
        <Alert variant="ok" role="status">
          {msg}
        </Alert>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="email">E-mail</Label>
            <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          {erro && <Alert variant="critico">{erro}</Alert>}
          <Button type="submit" className="w-full" disabled={busy}>
            <Mail aria-hidden />
            {busy ? "Enviando…" : "Enviar link"}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}
