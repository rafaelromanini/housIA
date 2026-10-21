"use client";

import { useState } from "react";
import { BadgeCheck, Send } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type User } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ROLE_LABEL } from "@/lib/format";
import { useApi } from "@/lib/use-api";

interface Convite {
  email: string;
  papel: string;
  aceito: boolean;
}

export function EquipeView() {
  const { user } = useAuth();
  const orgId = user?.organizacao?.id;
  const membros = useApi<User[]>(orgId ? `/orgs/${orgId}/membros` : null);
  const convites = useApi<Convite[]>(orgId ? `/orgs/${orgId}/invites` : null);
  const [email, setEmail] = useState("");
  const [papel, setPapel] = useState("enfermagem");
  const [msg, setMsg] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!orgId) return null;

  async function convidar(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    setMsg(null);
    try {
      await api(`/orgs/${orgId}/invites`, { method: "POST", json: { email, papel } });
      setMsg(`Convite enviado para ${email}.`);
      setEmail("");
      void convites.reload();
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível enviar o convite");
    } finally {
      setBusy(false);
    }
  }

  async function conferir(m: User, verificado: boolean) {
    setErro(null);
    try {
      await api(`/orgs/${orgId}/membros/${m.id}/conselho`, { method: "POST", json: { verificado } });
      void membros.reload();
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível atualizar o conselho");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Equipe de {user?.organizacao?.nome}</h1>
      {erro && <Alert variant="critico">{erro}</Alert>}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Convidar para a equipe</CardTitle>
          <CardDescription>A pessoa recebe um link por e-mail e entra com o papel escolhido aqui.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={convidar} className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end">
            <div className="space-y-1.5">
              <Label htmlFor="convite-email">E-mail</Label>
              <Input id="convite-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="convite-papel">Papel</Label>
              <NativeSelect id="convite-papel" value={papel} onChange={(e) => setPapel(e.target.value)}>
                {Object.entries(ROLE_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </NativeSelect>
            </div>
            <Button type="submit" disabled={busy}>
              <Send aria-hidden />
              Enviar convite
            </Button>
          </form>
          {msg && (
            <Alert variant="ok" role="status" className="mt-3">
              {msg}
            </Alert>
          )}
          {convites.data && convites.data.length > 0 && (
            <ul className="mt-3 space-y-1 text-sm" aria-label="Convites enviados">
              {convites.data.map((c, i) => (
                <li key={`${c.email}-${i}`}>
                  {c.email} · {ROLE_LABEL[c.papel] ?? c.papel} · {c.aceito ? "aceito" : "pendente"}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Profissionais</CardTitle>
          <CardDescription>
            Confira o registro no portal do conselho; só depois a nota exportada leva o selo “verificado”.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="divide-y">
            {(membros.data ?? []).map((m) => (
              <li key={m.id} className="flex flex-wrap items-center gap-3 py-2">
                <div className="min-w-0 flex-1">
                  <p className="font-bold">{m.name}</p>
                  <p className="text-sm text-muted-foreground">
                    {m.email} · {ROLE_LABEL[m.role]}
                    {m.conselho ? ` · ${m.conselho} ${m.conselho_numero}/${m.uf}` : ""}
                  </p>
                </div>
                {m.conselho_verificado ? (
                  <Badge variant="ok">
                    <BadgeCheck aria-hidden className="size-4" /> Verificado
                  </Badge>
                ) : m.conselho ? (
                  <Badge variant="atencao">Não verificado</Badge>
                ) : null}
                {m.conselho && (
                  <Button size="sm" variant="outline" onClick={() => conferir(m, !m.conselho_verificado)}>
                    {m.conselho_verificado ? "Remover selo" : "Marcar como conferido"}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
