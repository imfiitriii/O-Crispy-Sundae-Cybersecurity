const DEFAULTS = { enabled: true, backendUrl: "http://localhost:8000", largeText: false, readAloud: false, useGemini: true };
const ids = Object.keys(DEFAULTS);

async function checkServer(url) {
  const el = document.getElementById("status");
  try {
    const res = await fetch(url.replace(/\/+$/, "") + "/health", { headers: { "ngrok-skip-browser-warning": "1" } });
    const h = await res.json();
    el.className = "ok";
    el.textContent = `Connected · AI helper ${h.gemini ? "on" : "off"} · Fact-check ${h.fact_check ? "on" : "off"}`;
  } catch {
    el.className = "bad";
    el.textContent = "Can't reach the checking server.";
  }
}

(async () => {
  const s = { ...DEFAULTS, ...(await chrome.storage.sync.get(ids)) };
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el.type === "checkbox") el.checked = s[id];
    else el.value = s[id];
    el.addEventListener("change", async () => {
      const value = el.type === "checkbox" ? el.checked : el.value.trim();
      await chrome.storage.sync.set({ [id]: value });
      if (id === "backendUrl") checkServer(value);
    });
  }
  checkServer(s.backendUrl);
})();
