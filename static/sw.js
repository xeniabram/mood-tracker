self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let data = { title: "Mood Tracker", body: "Время записать настроение" };
  try {
    data = e.data.json();
  } catch {
    /* use defaults */
  }
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: "mood-reminder",
      renotify: true,
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window" }).then((cs) => {
      if (cs.length) return cs[0].focus();
      return self.clients.openWindow("/");
    })
  );
});
