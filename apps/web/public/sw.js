/* Service worker do HousIA: guarda SOMENTE o shell estático (ícones, manifesto, página offline).
 * Nunca intercepta nem armazena respostas da API, páginas autenticadas ou qualquer dado clínico:
 * só atende GET de mesma origem para a lista fechada abaixo; todo o resto passa direto para a rede. */
const CACHE = "housia-shell-v1";
const SHELL = ["/offline.html", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
    return;
  }
  // navegação sem rede: só a página offline genérica (sem dado de paciente); com rede, nada é guardado
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline.html")));
  }
});
