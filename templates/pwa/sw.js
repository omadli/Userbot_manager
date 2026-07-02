{% load static %}// Account Pro service worker.
//
// Kept deliberately small: enough to make the app installable and to show a
// friendly offline page, without trying to cache the whole (CDN-heavy) app.
// Bump CACHE when the shell assets below change so old caches are dropped.

const CACHE = 'accountpro-v1';
const OFFLINE_URL = '/offline/';
const SHELL = [
    OFFLINE_URL,
    '{% static "icons/icon-192.png" %}',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    // Leave cross-origin (CDN fonts/JS/CSS) to the browser.
    if (url.origin !== self.location.origin) return;

    // Page navigations: try the network, fall back to the offline page.
    if (req.mode === 'navigate') {
        event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
        return;
    }

    // Same-origin static files: cache-first, then network (and cache it).
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(req).then((hit) => {
                if (hit) return hit;
                return fetch(req).then((res) => {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put(req, copy));
                    return res;
                });
            })
        );
    }
});
