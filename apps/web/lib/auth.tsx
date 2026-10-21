"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { api, ApiError, endSession, type User } from "@/lib/api";
import { garantirDono, limparTudo } from "@/lib/offline-queue";

interface AuthState {
  user: User | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<User>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const router = useRouter();

  useEffect(() => {
    // A sessão vive num cookie httpOnly: quem decide se há sessão é o servidor.
    let vivo = true;
    api<User>("/auth/me")
      .then(async (u) => {
        await garantirDono(u.id);
        if (vivo) setUser(u);
      })
      .catch((e) => {
        if (!(e instanceof ApiError) || e.status !== 401) console.warn("Falha ao consultar a sessão", e);
      })
      .finally(() => vivo && setReady(true));
    return () => {
      vivo = false;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<{ user: User }>("/auth/login", {
      method: "POST",
      json: { email, password },
    });
    await garantirDono(res.user.id); // outro usuário no mesmo aparelho não herda a fila
    setUser(res.user);
    return res.user;
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    void limparTudo(); // falas pendentes na fila offline não sobrevivem à saída da conta
    void endSession().then(() => router.push("/login"));
  }, [router]);

  const value = useMemo(() => ({ user, ready, login, logout }), [user, ready, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth precisa de <AuthProvider>");
  return ctx;
}
