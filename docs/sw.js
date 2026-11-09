/* AI Desk Bot: lock-screen notifications (Web Push).
   The bot sends {title, body, url, tag}; this shows it and, on a tap, opens the app at that page.
   iOS requires a visible notification for every push, so one is always shown. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; }
  catch (e) { msg = {body: event.data ? event.data.text() : ""}; }
  const title = String(msg.title || "AI Desk Bot").slice(0, 120);
  event.waitUntil(self.registration.showNotification(title, {
    body: String(msg.body || "").slice(0, 300),
    icon: "icon-180.png",
    badge: "icon-180.png",
    tag: msg.tag ? String(msg.tag).slice(0, 80) : undefined,
    data: {url: typeof msg.url === "string" ? msg.url : "./"},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "./", self.registration.scope);
  if (target.origin !== self.location.origin) return;   // only pages of this app
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const w of wins) {
      if ("focus" in w) {
        try { await w.navigate(target.href); } catch (e) {}
        return w.focus();
      }
    }
    return self.clients.openWindow(target.href);
  })());
});
