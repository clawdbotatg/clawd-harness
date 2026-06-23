// clawd-harness PWA service worker — NETWORK-ONLY passthrough.
//
// Its ONLY job is to make the app installable (Chrome/Android require a fetch
// handler; iOS add-to-home-screen works off the manifest + apple-touch-icon).
// It deliberately does NOT cache anything: the app is useless offline (it's a
// live PTY mirror), and caching HTML would fight the harness's live-reload
// (WS {type:"reload"}) + the `Cache-Control: no-store` headers the server/relay
// already send. So we take over the scope only to claim installability, and let
// every request fall through to the normal network path.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// No respondWith() → the browser performs its default network fetch, uncached.
self.addEventListener('fetch', () => {});

// Web Push: the worker sends a BODYLESS tickle (no payload — keeps session
// content off the wire and clear of the E2E boundary), so the banner text lives
// here, not in the message. If a payload ever is attached, prefer it.
self.addEventListener('push', (e) => {
  let title = 'clawd', body = 'a session needs you', url = '/';
  try {
    if (e.data) {
      const d = e.data.json();   // encrypted payload from the worker (if present)
      title = d.title || title;
      body = d.body || body;
      url = d.url || url;        // deep link to the session that needs you
    }
  } catch (_) { /* bodyless tickle → generic banner, opens at the roster */ }
  e.waitUntil(self.registration.showNotification(title, {
    body,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: 'clawd-attention',   // collapse a burst into one banner
    renotify: true,
    data: { url },
  }));
});

// Tap → navigate to the session's deep link: focus an open window and route it
// there (its hashchange router handles the rest), else open a new one at the URL.
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) {
        try { if ('navigate' in c) await c.navigate(url); } catch (_) {}
        return c.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  })());
});
