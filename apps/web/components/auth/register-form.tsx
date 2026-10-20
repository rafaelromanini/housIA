"use client";

import Link from "next/link";
import { useState } from "react";
import { UserPlus } from "lucide-react";

import { AuthCard, linkClass } from "@/components/auth/auth-card";
import { ConselhoFields, type Conselho } from "@/components/auth/conselho-fields";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api } from "@/lib/api";

export function RegisterForm({ tipoInicial }: { tipoInicial: "consultorio" | "hospital" }) {
  const [tipo, setTipo] = useState<"consultorio" | "hospital">(tipoInicial);
  const [nome, setNome] = useState("");
  const [org, setOrg] = useState("");
  const [email, setEmail] = useState("");
  const [senha, setSenha] = useState("");
  const [cons, setCons] = useState<Conselho>({ conselho: "CRM", numero: "", uf: "" });
  const [erro, setErro] = useState<string | null>(null);
  const [enviado, setEnviado] = useState(false);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    try {
      const informou = cons.numero || cons.uf;
      await api("/auth/register", {
        method: "POST",
        json: {
          nome,
          email,
          senha,
          organizacao_nome: org,
          organizacao_tipo: tipo,
          conselho: tipo === "consultorio" || informou ? cons.conselho : null,
          conselho_numero: tipo === "consultorio" || informou ? cons.numero : null,
          uf: tipo === "consultorio" || informou ? cons.uf : null,
        },
      });
      setEnviado(true);
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível criar a conta");
    } finally {
      setBusy(false);
    }
  }

  if (enviado) {
    return (
      <AuthCard titulo="Confira seu e-mail" descricao="Enviamos um link para confirmar o cadastro.">
        <Alert variant="info" role="status">
          Abra a mensagem enviada para <strong>{email}</strong> e clique no link. Depois é só entrar.
        </Alert>
        <Link href="/login" className={linkClass}>
          Ir para o login
        </Link>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      titulo="Começar grátis"
      descricao="Crie sua conta e a organização em um minuto."
      rodape={
        <p>
          Já tem conta?{" "}
          <Link href="/login" className={linkClass}>
            Entrar
          </Link>
        </p>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="tipo">Tipo de organização</Label>
          <NativeSelect id="tipo" value={tipo} onChange={(e) => setTipo(e.target.value as "consultorio" | "hospital")}>
            <option value="consultorio">Consultório ou clínica</option>
            <option value="hospital">Hospital</option>
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="org">{tipo === "consultorio" ? "Nome do consultório" : "Nome do hospital"}</Label>
          <Input id="org" value={org} onChange={(e) => setOrg(e.target.value)} required minLength={2} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="nome">Seu nome</Label>
          <Input id="nome" autoComplete="name" value={nome} onChange={(e) => setNome(e.target.value)} required minLength={3} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email">E-mail</Label>
          <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="senha">Senha (mínimo de 8 caracteres)</Label>
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
        <ConselhoFields value={cons} onChange={setCons} obrigatorio={tipo === "consultorio"} />
        {erro && <Alert variant="critico">{erro}</Alert>}
        <Button type="submit" className="w-full" disabled={busy}>
          <UserPlus aria-hidden />
          {busy ? "Criando…" : "Criar conta"}
        </Button>
      </form>
    </AuthCard>
  );
}
