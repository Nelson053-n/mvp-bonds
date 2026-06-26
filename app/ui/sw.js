const CACHE_NAME = 'bond-ai-v110';
const STATIC_ASSETS = [
  '/manifest.json',
  '/static/translations.js?v=110',
  '/static/styles.css?v=110',
  '/static/app.js?v=110',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Drop every cache that isn't the current version — old HTML/JS can't survive.
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)));
    // Speed up network-first navigation when supported.
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) {}
    }
    await self.clients.claim();
  })());
});

// Let the page ask which version is in control, so a page served fresh from the
// network can detect it's being controlled by a stale SW and force an update.
self.addEventListener('message', (event) => {
  if (event.data === 'GET_VERSION' && event.source) {
    event.source.postMessage({ type: 'SW_VERSION', version: CACHE_NAME });
  }
  if (event.data === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Leave cross-origin requests (Google Fonts, Yandex Metrika, …) to the browser.
  // Wrapping them in our own fetch() would subject them to the page's CSP
  // connect-src directive, which blocks third-party hosts allowed only for
  // style-src/font-src/script-src.
  if (url.origin !== self.location.origin) return;

  // Never serve sw.js from cache — always hit the network so a stale worker
  // can't pin itself in place. (The app shell /app is handled in the navigate
  // branch below so the navigation-preload response is always consumed.)
  if (url.pathname === '/sw.js') {
    event.respondWith(fetch(event.request, { cache: 'no-store' }).catch(() =>
      caches.match(event.request)
    ));
    return;
  }

  // Network-first for API calls
  if (url.pathname.startsWith('/api') ||
      url.pathname.startsWith('/portfolios') ||
      url.pathname.startsWith('/auth') ||
      url.pathname.startsWith('/settings') ||
      url.pathname.startsWith('/bonds') ||
      url.pathname.startsWith('/admin')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Network-first for HTML; uses the navigation preload response when available,
  // and {cache:'no-cache'} revalidates with the server so a stale browser
  // HTTP-cache entry (public pages have max-age) can't pin old HTML.
  if (event.request.mode === 'navigate') {
    // /app must always come fresh from the network (no-store) so a stale shell
    // can't pin an old page; it ignores the preload response, but we still must
    // consume event.preloadResponse, otherwise the browser cancels the preload
    // request before it settles and logs a console warning.
    const isAppShell = url.pathname === '/app';
    event.respondWith((async () => {
      // Always settle the preload promise so it isn't left dangling.
      const preload = await event.preloadResponse.catch(() => null);
      try {
        if (isAppShell) {
          return await fetch(event.request, { cache: 'no-store' });
        }
        if (preload) return preload;
        return await fetch(event.request, { cache: 'no-cache' });
      } catch (e) {
        // Offline / network error: serve whatever shell we have, else a minimal
        // response so respondWith() never resolves to undefined (which the browser
        // logs as "FetchEvent resulted in a network error response").
        return (await caches.match('/app')) ||
               (await caches.match('/')) ||
               new Response('', { status: 503, statusText: 'Offline' });
      }
    })());
    return;
  }

  // Cache-first for everything else. fetch() may reject on a network error, so
  // catch it and fall back to the cache (or a 503) — otherwise the rejection
  // surfaces as an uncaught "TypeError: Failed to fetch" in the console.
  event.respondWith(
    caches.match(event.request).then((cached) =>
      cached || fetch(event.request).catch(() =>
        new Response('', { status: 503, statusText: 'Offline' })
      )
    )
  );
});
