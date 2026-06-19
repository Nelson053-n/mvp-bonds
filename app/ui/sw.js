const CACHE_NAME = 'bond-ai-v97';
const STATIC_ASSETS = [
  '/manifest.json',
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

  // Never serve sw.js or the app shell from cache — always hit the network so a
  // stale worker can't pin itself or an old page in place.
  if (url.pathname === '/sw.js' || url.pathname === '/app') {
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
    event.respondWith((async () => {
      try {
        const preload = await event.preloadResponse;
        if (preload) return preload;
        return await fetch(event.request, { cache: 'no-cache' });
      } catch (e) {
        return (await caches.match('/app')) || (await caches.match('/'));
      }
    })());
    return;
  }

  // Cache-first for everything else
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
