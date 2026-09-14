// Background push handler. This has to be served from the site ROOT
// (see the route in app/main.py), because a service worker can only
// control pages at or below its own path - served from /static it
// would never receive a background notification.

importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js");
importScripts("/firebase-config.js");

if (typeof firebaseConfig !== "undefined" && firebaseConfig.apiKey) {
  firebase.initializeApp(firebaseConfig);
  const messaging = firebase.messaging();

  messaging.onBackgroundMessage((payload) => {
    const n = payload.notification || {};
    self.registration.showNotification(n.title || "Don't Miss The Moment", {
      body: n.body || "",
      icon: "/icon.png",
      // Carry the stream link through, because tapping the
      // notification and landing on the event is the entire point.
      data: { link: (payload.data && payload.data.link) || "/" },
    });
  });
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const link = (event.notification.data && event.notification.data.link) || "/";
  event.waitUntil(clients.openWindow(link));
});
