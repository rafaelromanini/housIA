"use client";

import { useState } from "react";
import { Send } from "lucide-react";

import { AuthCard } from "@/components/auth/auth-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";

export function PilotForm() {
  const [f, setF] = useState({ hospital: "", contato: "", email: "", leitos: "", mensagem: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setF((o) => ({ ...o, [k]: e.target.value }));

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErro(null);
    try {
      const r = await api<{ detail: string }>("/public/piloto", {
        method: "POST",
        json: { ...f, leitos: f.leitos ? Number(f.leitos) : null },
      });
      setMsg(r.detail);
    } catch (err) {
      setErro(err instanceof Error ? err.message : "Não foi possível enviar o pedido");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard titulo="Piloto para hospital" descricao="Conte um pouco sobre o hospital; a equipe do HousIA responde por e-mail.">
      {msg ? (
        <Alert variant="ok" role="status">
          {msg}
        </Alert>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="hospital">Hospital</Label>
            <Input id="hospital" value={f.hospital} onChange={set("hospital")} required minLength={2} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="contato">Seu nome</Label>
            <Input id="contato" autoComplete="name" value={f.contato} onChange={set("contato")} required minLength={3} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="email">E-mail</Label>
            <Input id="email" type="email" autoComplete="email" value={f.email} onChange={set("email")} required />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="leitos">Número de leitos (opcional)</Label>
            <Input id="leitos" type="number" min={1} max={5000} value={f.leitos} onChange={set("leitos")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mensagem">Mensagem (opcional)</Label>
            <Textarea id="mensagem" maxLength={1000} value={f.mensagem} onChange={set("mensagem")} />
          </div>
          {erro && <Alert variant="critico">{erro}</Alert>}
          <Button type="submit" className="w-full" disabled={busy}>
            <Send aria-hidden />
            {busy ? "Enviando…" : "Pedir piloto"}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}
