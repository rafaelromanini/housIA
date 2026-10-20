"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { UserPlus } from "lucide-react";

import { AuthCard, linkClass } from "@/components/auth/auth-card";
import { ConselhoFields, type Conselho } from "@/components/auth/conselho-fields";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { ROLE_LABEL } from "@/lib/format";

interface Convite {
  email: string;
  papel: string;
  organizacao: string;
}

export function InviteForm() {
  const token = useSearchParams().get("token") ?? "";
  const [convite, setConvite] = useState<Convite | null>(null);
  const [nome, setNome] = useState("");
  const [senha, setSenha] = useState("");
  const [cons, setCons] = useState<Conselho>({ conselho: "", numero: "", uf: "" });
  const [erro, setErro] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let vivo = true;
    if (!token) {
      setErro("Link incompleto.");
      return;
    }
    api<Convite>(`/auth/invites/${encodeURIComponent(token)}`)
      .then((c) => vivo && setConvite(c))
      .catch((e) => vivo && setErro(e instanceof Error ? e.message : "Convite inválido"));
    return () => {
      vivo = false;
    };
  }, [token]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    const informou = cons.conselho || cons.numero || cons.uf;
    try {
      await api("/auth/invites/accept", {
        method: "POST",
        json: {
          token,
          nome,
          senha,
          conselho: informou ? cons.conselho : null,
          conselho_numero: informou ? cons.numero : null,
          uf: informou ? cons.uf : null,
        },
      });
      setOk(true);
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível criar a conta");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard
      titulo="Convite para a equipe"
      descricao={convite ? `${convite.organizacao} convidou ${convite.email} como ${ROLE_LABEL[convite.papel] ?? convite.papel}.` : undefined}
    >
      {ok ? (
        <>
          <Alert variant="ok" role="status">
            Conta criada.
          </Alert>
          <Link href="/login" className={linkClass}>
            Entrar
          </Link>
        </>
      ) : (
        <>
          {erro && <Alert variant="critico">{erro}</Alert>}
          {convite && (
            <form onSubmit={onSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="nome">Seu nome</Label>
                <Input id="nome" autoComplete="name" value={nome} onChange={(e) => setNome(e.target.value)} required minLength={3} />
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
              <ConselhoFields value={cons} onChange={setCons} obrigatorio={false} />
              <Button type="submit" className="w-full" disabled={busy}>
                <UserPlus aria-hidden />
                {busy ? "Criando…" : "Criar conta"}
              </Button>
            </form>
          )}
        </>
      )}
    </AuthCard>
  );
}
