/* Market Radar service worker.
   Shell is cached so the app opens instantly and works offline.
   Data is always fetched network-first: a stale price shown as current
   would be worse than showing nothing. */

const SHELL = "mr-shell-v5";
const SHELL_FILES = ["./", "./index.html", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  const isData = req.url.includes("/data/");

  if (isData) {
    // Network first. Fall back to cache only when offline, so the page can
    // still render something -- the "as of" dates in the UI tell the reader
    // how old it is.
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
});
