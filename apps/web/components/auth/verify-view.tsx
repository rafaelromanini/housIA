"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { AuthCard, linkClass } from "@/components/auth/auth-card";
import { Alert } from "@/components/ui/alert";
import { api } from "@/lib/api";

export function VerifyView() {
  const token = useSearchParams().get("token") ?? "";
  const [estado, setEstado] = useState<"verificando" | "ok" | "erro">("verificando");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    let vivo = true;
    if (!token) {
      setEstado("erro");
      setMsg("Link incompleto.");
      return;
    }
    api<{ detail: string }>("/auth/verify", { method: "POST", json: { token } })
      .then((r) => vivo && (setMsg(r.detail), setEstado("ok")))
      .catch((e) => vivo && (setMsg(e instanceof Error ? e.message : "Falha na verificação"), setEstado("erro")));
    return () => {
      vivo = false;
    };
  }, [token]);

  return (
    <AuthCard titulo="Confirmação de e-mail">
      {estado === "verificando" && <p role="status">Confirmando…</p>}
      {estado === "ok" && (
        <Alert variant="ok" role="status">
          {msg}
        </Alert>
      )}
      {estado === "erro" && <Alert variant="critico">{msg}</Alert>}
      <Link href="/login" className={linkClass}>
        Ir para o login
      </Link>
    </AuthCard>
  );
}
