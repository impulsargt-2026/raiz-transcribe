/* RAÍZ Transcribe — service worker (solo HTTPS). Guarda la "cáscara" de la app para abrirla aunque el
   servidor esté dormido o no haya señal: se puede GRABAR igual (queda en IndexedDB) y se sube después.
   Las llamadas /api/* nunca se cachean. */
const CACHE = "raiz-shell-v1";
const SHELL = ["/app.js", "/recorder.js", "/icon-180.png", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });

function withTimeout(p, ms) { return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]); }

self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/")) return;
  if (e.request.mode === "navigate") {   // página: red primero (clave/HTML frescos), si no responde en 6 s → copia
    e.respondWith(withTimeout(fetch(e.request), 6000).then(r => {
      if (r.ok && (r.headers.get("content-type") || "").includes("text/html") && !r.headers.get("x-render-routing")) {
        const copy = r.clone(); caches.open(CACHE).then(c => c.put("/", copy));
      }
      return r;
    }).catch(() => caches.match("/").then(r => r || new Response("RAÍZ Transcribe: sin conexión y sin copia local todavía. Abrí la app una vez con conexión.", {headers: {"content-type": "text/plain; charset=utf-8"}}))));
    return;
  }
  if (SHELL.includes(u.pathname)) {      // recursos: red, y si falla, copia
    e.respondWith(withTimeout(fetch(e.request), 6000).then(r => { if (r.ok) { const c2 = r.clone(); caches.open(CACHE).then(c => c.put(e.request, c2)); } return r; })
      .catch(() => caches.match(e.request)));
  }
});
