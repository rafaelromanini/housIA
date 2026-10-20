"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { KeyRound } from "lucide-react";

import { AuthCard, linkClass } from "@/components/auth/auth-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";

export function ResetForm() {
  const token = useSearchParams().get("token") ?? "";
  const [senha, setSenha] = useState("");
  const [ok, setOk] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    try {
      await api("/auth/reset", { method: "POST", json: { token, senha } });
      setOk(true);
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível redefinir a senha");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard titulo="Nova senha" descricao="Escolha uma senha com ao menos 8 caracteres.">
      {ok ? (
        <>
          <Alert variant="ok" role="status">
            Senha redefinida.
          </Alert>
          <Link href="/login" className={linkClass}>
            Entrar
          </Link>
        </>
      ) : !token ? (
        <Alert variant="critico">Link incompleto. Peça um novo em “Esqueci a senha”.</Alert>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="senha">Nova senha</Label>
            <Input
              id="senha"
              type="password"
              autoComplete="new-password"
              value={senha}
              onChange={(e) => setSenha(e.target.value)}
              required
              minLength={8}
            />
          </div>
          {erro && <Alert variant="critico">{erro}</Alert>}
          <Button type="submit" className="w-full" disabled={busy}>
            <KeyRound aria-hidden />
            {busy ? "Salvando…" : "Redefinir senha"}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}
