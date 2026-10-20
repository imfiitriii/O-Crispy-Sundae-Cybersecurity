// ScrollSafe background worker: talks to the backend and takes tab screenshots.
// Requests go through here (not the page) so TikTok/Instagram's security rules
// don't block calls to a localhost or ngrok backend.

const DEFAULTS = { enabled: true, backendUrl: "http://localhost:8000", largeText: false, readAloud: false, useGemini: true };

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.sync.get(Object.keys(DEFAULTS))) };
}

async function userId() {
  const { userId } = await chrome.storage.local.get("userId");
  if (userId) return userId;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ userId: id });
  return id;
}

async function api(path, options = {}) {
  const { backendUrl } = await settings();
  const res = await fetch(backendUrl.replace(/\/+$/, "") + path, {
    ...options,
    // ngrok shows a warning page unless this header is set.
    headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "1", ...(options.headers || {}) },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Backend error ${res.status}`);
  return res.json();
}

async function handle(msg, sender) {
  switch (msg.type) {
    case "settings":
      return settings();
    case "lookup":
      return api(`/verdict/${encodeURIComponent(msg.platform)}/${encodeURIComponent(msg.videoId)}`);
    case "analyze":
      return api("/analyze", { method: "POST", body: JSON.stringify(msg.payload) });
    case "report":
      return api("/report", {
        method: "POST",
        body: JSON.stringify({ platform: msg.platform, video_id: msg.videoId, label: msg.label, user_id: await userId() }),
      });
    case "capture":
      // Fallback when the video can't be read directly: screenshot the visible tab.
      return chrome.tabs.captureVisibleTab(sender.tab.windowId, { format: "jpeg", quality: 80 });
    default:
      throw new Error("Unknown message " + msg.type);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg, sender)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: String(err && err.message ? err.message : err) }));
  return true; // keep the channel open for the async response
});
