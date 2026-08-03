/* ClaudeCoach service worker.
 *
 * Lives at /ClaudeCoach/sw.js and NOT in app/ on purpose: a worker's default scope is its
 * own directory, so app/sw.js could only ever control app/ and would never see the pages
 * it exists to serve. GitHub Pages cannot send a Service-Worker-Allowed header to widen
 * scope, so the file's location IS the scope.
 *
 * Strategy differs by what the request is FOR, because the wrong choice here is worse than
 * no worker at all:
 *   app shell + icons  cache-first        - versioned by CACHE, changes only on deploy
 *   HTML pages         network-first      - stale training data is misleading, so always
 *                                          try the network; fall back to cache offline
 *   training data JSON network-first      - same reasoning, more so
 *   CDN (fonts, chart) stale-while-revalidate - third-party, immutable in practice, and
 *                                          the pages are unreadable without them offline
 *
 * Bump CACHE on every deploy that changes a precached file.
 */
const CACHE = 'claudecoach-v3';

const SHELL = [
  './',
  './app.html',
  './index.html',
  './app/app.js',
  './app/app-shell.css',
  './app/app-shell.js',
  './app/manifest.webmanifest',
  './app/icons/icon-192.png',
  './app/icons/icon-512.png',
  './app/icons/apple-touch-icon.png',
  './app/icons/favicon-32.png',
  './public/session-library.json',
];

const CDN_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // addAll rejects the whole install if ANY entry 404s, which would leave the app
      // permanently without a worker. Add individually and tolerate misses.
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(request) {
  return fetch(request)
    .then((res) => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(request, copy));
      }
      return res;
    })
    .catch(() => caches.match(request).then((hit) => hit || offlineFallback(request)));
}

function cacheFirst(request) {
  return caches.match(request).then((hit) => hit || fetch(request).then((res) => {
    if (res && res.ok) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(request, copy));
    }
    return res;
  }));
}

function staleWhileRevalidate(request) {
  return caches.match(request).then((hit) => {
    const net = fetch(request).then((res) => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(request, copy));
      }
      return res;
    }).catch(() => hit);
    return hit || net;
  });
}

function offlineFallback(request) {
  // A navigation that misses entirely still gets the hub rather than the browser's
  // dinosaur, so the app never looks broken.
  if (request.mode === 'navigate') {
    return caches.match('./app.html');
  }
  return Response.error();
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  if (CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Only handle our own origin beyond this point.
  if (url.origin !== self.location.origin) return;
  // And only our own scope - the rest of diamondpeak.uk is not this app.
  if (!url.pathname.startsWith('/ClaudeCoach/')) return;

  const isShell = /\/(app\/|sw\.js)/.test(url.pathname);
  if (isShell) {
    event.respondWith(cacheFirst(request));
    return;
  }

  event.respondWith(networkFirst(request));
});
