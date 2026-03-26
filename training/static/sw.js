// Service Worker for annotation push notifications
// Polls for new review packets and shows browser notifications

const POLL_INTERVAL = 5 * 60 * 1000; // 5 minutes
let lastKnownPending = -1; // -1 = not yet checked

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// Listen for messages from the page
self.addEventListener("message", (e) => {
  if (e.data === "start-polling") {
    pollForPackets();
  }
});

async function pollForPackets() {
  try {
    const resp = await fetch("/api/packets");
    const packets = await resp.json();

    const pending = packets.filter((p) => p.status !== "complete");
    const pendingFrames = pending.reduce(
      (sum, p) => sum + (p.frame_count - p.reviewed_count),
      0
    );

    // Only notify if there are new pending packets since last check
    if (lastKnownPending >= 0 && pending.length > lastKnownPending) {
      const newCount = pending.length - lastKnownPending;
      await self.registration.showNotification("New review packets ready", {
        body: `${newCount} new packet${newCount > 1 ? "s" : ""} with ${pendingFrames} frames to review`,
        icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚽</text></svg>",
        tag: "new-packets",
        renotify: true,
        data: { url: "/" },
      });
    }

    lastKnownPending = pending.length;
  } catch (e) {
    // Silently ignore fetch errors (server might be down)
  }

  // Schedule next poll
  setTimeout(pollForPackets, POLL_INTERVAL);
}

// Open the app when notification is tapped
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        if (clients.length > 0) {
          return clients[0].focus();
        }
        return self.clients.openWindow(e.notification.data?.url || "/");
      })
  );
});
