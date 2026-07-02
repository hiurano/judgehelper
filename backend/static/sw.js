// Service worker — cache static assets ONLY, never the auth-protected main HTML.
// Why: before this fix, the SW cached "/" and served it on subsequent visits
// without hitting the backend middleware, so a logged-out user could see the
// app UI without being asked for credentials. Then any API call (upload,
// status) would 401, losing in-memory state.
//
// IMPORTANT: bump CACHE version to force clients to drop the stale cache.
const CACHE = 'judge-helper-v3';
const STATIC = ['/manifest.json', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', (e) => {
    e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)));
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (e) => {
    const url = new URL(e.request.url);

    // Always hit the network for HTML navigation — so middleware can redirect
    // unauthenticated users to /login. Never serve cached "/" or "/login".
    if (e.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/login') {
        return; // network only
    }

    // API endpoints — always network
    if (
        url.pathname === '/upload' ||
        url.pathname.startsWith('/status/') ||
        url.pathname.startsWith('/download/') ||
        url.pathname.startsWith('/render-docx') ||
        url.pathname === '/webhook/aai' ||
        url.pathname === '/health' ||
        url.pathname === '/logout'
    ) {
        return; // pass-through
    }

    // Static files (icons, manifest) — cache-first
    e.respondWith(
        caches.match(e.request).then((cached) => cached || fetch(e.request))
    );
});
