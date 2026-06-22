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
