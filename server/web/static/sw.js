// Minimal service worker — pre-caches the page shell (CSS, htmx, pepper
// icon, manifest) so the installed PWA loads fast and works briefly
// offline. HTML and API responses are NEVER cached: the library is
// dynamic (search, sort, ratings, comments) and we'd rather show a
// network error than stale data.

const CACHE = 'epepper-shell-v1';
const SHELL = [
  '/app/static/app.css',
  '/app/static/htmx.min.js',
  '/app/static/pepper.svg',
  '/app/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Only intercept GETs on our own origin to the static shell — leave
  // everything else (HTML pages, /image, /version, HTMX partials) to the
  // network so the user always sees current state.
  if (event.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/app/static/')) return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
