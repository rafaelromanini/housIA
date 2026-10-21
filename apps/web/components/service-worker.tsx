"use client";

import { useEffect } from "react";

/** Registra o service worker do shell (ver public/sw.js: só ícones, manifesto e página offline; nunca dado clínico). */
export function ServiceWorker() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production" || !("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      /* sem service worker o app segue funcionando; só não é instalável */
    });
  }, []);
  return null;
}
