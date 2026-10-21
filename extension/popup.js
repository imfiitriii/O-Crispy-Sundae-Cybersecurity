const DEFAULTS = { enabled: true, backendUrl: "http://localhost:8000", largeText: false, readAloud: false, useGemini: true, lang: "en" };
const ids = Object.keys(DEFAULTS);

const STR = {
  en: {
    tagline: "Warns you about deepfakes, AI videos, scams and false claims.",
    enabled: "Turn on ScrollSafe", large: "Large text", largeD: "Bigger words on the badge",
    aloud: "Read warnings aloud", aloudD: "Speaks red warnings",
    helper: "AI helper (Gemini)", helperD: "More accurate, but slower",
    server: "Checking server", connecting: "Checking connection…",
    on: "on", off: "off",
    connected: (g, f) => `Connected · AI helper ${g} · Fact-check ${f}`,
    unreachable: "Can't reach the checking server.",
  },
  ms: {
    tagline: "Memberi amaran tentang deepfake, video AI, penipuan dan dakwaan palsu.",
    enabled: "Hidupkan ScrollSafe", large: "Tulisan besar", largeD: "Perkataan lebih besar pada lencana",
    aloud: "Bacakan amaran", aloudD: "Membacakan amaran merah",
    helper: "Pembantu AI (Gemini)", helperD: "Lebih tepat, tetapi lebih perlahan",
    server: "Pelayan semakan", connecting: "Menyemak sambungan…",
    on: "hidup", off: "mati",
    connected: (g, f) => `Bersambung · Pembantu AI ${g} · Semakan fakta ${f}`,
    unreachable: "Tidak dapat menghubungi pelayan semakan.",
  },
};
let lang = "en";
let lastHealth = null;  // null = not checked yet, false = unreachable
const t = (k) => (STR[lang] || STR.en)[k];

function applyLang() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.lang === lang));
  showStatus();
}

function showStatus() {
  const el = document.getElementById("status");
  if (lastHealth === null) return;
  if (lastHealth === false) {
    el.className = "bad";
    el.textContent = t("unreachable");
    return;
  }
  el.className = "ok";
  el.textContent = t("connected")(t(lastHealth.gemini ? "on" : "off"), t(lastHealth.fact_check ? "on" : "off"));
}

async function checkServer(url) {
  try {
    const res = await fetch(url.replace(/\/+$/, "") + "/health", { headers: { "ngrok-skip-browser-warning": "1" } });
    lastHealth = await res.json();
  } catch {
    lastHealth = false;
  }
  showStatus();
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
      if (id === "lang") { lang = value; applyLang(); }
    });
  }
  // Language buttons write into the hidden #lang input, which is saved like the other settings.
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    const input = document.getElementById("lang");
    input.value = b.dataset.lang;
    input.dispatchEvent(new Event("change"));
  }));
  lang = s.lang;
  applyLang();
  checkServer(s.backendUrl);
})();
