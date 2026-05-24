// Minimal service worker — pre-caches the page shell (CSS, htmx, pepper
// icon, manifest) so the installed PWA loads fast and works briefly
// offline. HTML and API responses are NEVER cached: the library is
// dynamic (search, sort, comments) and we'd rather show a network error
// than stale data.

// Bump CACHE to force-invalidate the old cache on next activation —
// the activate handler below deletes any cache whose name doesn't
// match this constant. Bumping is only required when the cache key
// strategy changes; routine CSS / JS edits now ride the
// stale-while-revalidate path below and roll out on the second load
// after deploy.
const CACHE = 'epepper-shell-v4';
const SHELL = [
  '/app/static/app.css',
  '/app/static/htmx.min.js',
  '/app/static/input-action.js',
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

  // Stale-while-revalidate: serve cache instantly (fast) AND refetch
  // in the background so the next load picks up the new file. Prior
  // strategy was cache-first with no refresh, which froze users on
  // whatever shell was current at install time and required a CACHE
  // version bump for every CSS tweak to ship.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      const refresh = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            cache.put(event.request, response.clone());
          }
          return response;
        })
        .catch(() => null);
      return cached || refresh;
    })
  );
});
