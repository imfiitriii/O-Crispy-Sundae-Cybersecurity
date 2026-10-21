// ScrollSafe content script: runs on TikTok and Instagram.
// Finds the video you're watching, grabs a few frames, asks the backend, and shows a badge.

(() => {
  const DWELL_MS = 2500;      // wait this long on a video before checking (people swipe past most videos)
  const FRAME_COUNT = 5;
  const FRAME_GAP_MS = 650;   // frames are taken while the video plays
  const FRAME_MAX_SIDE = 512;
  const TICK_MS = 500;

  const platform = location.hostname.includes("tiktok") ? "tiktok" : "instagram";
  const results = new Map();  // videoKey -> verdict (this page only; the backend keeps the shared cache)
  let settings = { enabled: true, largeText: false, readAloud: false, useGemini: true, lang: "en" };

  // ---------- languages ----------
  const STR = {
    en: {
      checking: "Checking…", why: "Why?", hide: "Hide details", read: "Read", report: "Report",
      reported: "Reported, thank you", reportFail: "Couldn't report", smaller: "Make smaller",
      deepfake: "Deepfake", aiMade: "AI-made", helperOff: "AI helper off: local checks only",
      reports: (n) => `${n} community report(s)`, rated: (p, r) => `${p || "Fact-checker"}: rated ${r}`,
      cantCheck: "Couldn't check", serverDown: "The checking server isn't reachable.",
    },
    ms: {
      checking: "Sedang menyemak…", why: "Kenapa?", hide: "Sembunyi", read: "Baca", report: "Laporkan",
      reported: "Sudah dilaporkan, terima kasih", reportFail: "Tidak dapat melapor", smaller: "Kecilkan",
      deepfake: "Deepfake", aiMade: "Dibuat AI", helperOff: "Pembantu AI dimatikan: semakan asas sahaja",
      reports: (n) => `${n} laporan pengguna`, rated: (p, r) => `${p || "Penyemak fakta"}: dinilai ${r}`,
      cantCheck: "Tidak dapat disemak", serverDown: "Pelayan semakan tidak dapat dihubungi.",
    },
  };
  const t = (k) => (STR[settings.lang] || STR.en)[k];
  // Verdicts carry their text in both languages (v.i18n); older saved verdicts only have English.
  const loc = (v) => ({ ...v, ...(v.i18n?.[settings.lang] || {}) });
  let current = null;         // { key, video, since, state }
  let busy = false;

  // ---------- messaging ----------
  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => resolve(res || { ok: false, error: chrome.runtime.lastError?.message }));
      } catch (e) {
        resolve({ ok: false, error: String(e) }); // extension was reloaded
      }
    });
  }

  // ---------- finding the active video ----------
  function visibleArea(el) {
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    return w * h;
  }

  function activeVideo() {
    let best = null, bestArea = 0;
    for (const v of document.querySelectorAll("video")) {
      const area = visibleArea(v) * (v.paused ? 0.5 : 1); // prefer the one that's playing
      if (area > bestArea && v.getBoundingClientRect().height > 150) { best = v; bestArea = area; }
    }
    return best;
  }

  // Walk up from the video to find the post container and its ID.
  function videoInfo(video) {
    const idFromHref = (href) => {
      const m = href && (href.match(/\/video\/(\d{8,})/) || href.match(/\/reels?\/([\w-]{5,})/) || href.match(/\/p\/([\w-]{5,})/));
      return m ? m[1] : null;
    };
    let id = null, container = video;
    let el = video;
    for (let i = 0; i < 14 && el && el !== document.body; i++, el = el.parentElement) {
      const idAttr = el.id && el.id.match(/(\d{15,})/);
      if (idAttr) { id = idAttr[1]; container = el; break; }
      const link = el.querySelector('a[href*="/video/"], a[href*="/reel/"], a[href*="/reels/"]');
      if (link && idFromHref(link.getAttribute("href"))) { id = idFromHref(link.getAttribute("href")); container = el; break; }
      if (i === 8) container = el;
    }
    // On a single-video page (or IG /reels/<code>/) the URL has the ID.
    const urlId = idFromHref(location.pathname);
    const onlyOneVideo = document.querySelectorAll("video").length <= 1;
    if (urlId && (!id || onlyOneVideo || platform === "instagram")) id = urlId;

    const q = (sel) => container.querySelector(sel)?.innerText?.trim() || "";
    let caption = q('[data-e2e="video-desc"], [data-e2e="browse-video-desc"], h1') ||
                  (container.innerText || "").trim();
    caption = caption.slice(0, 1200);
    const author = q('[data-e2e="video-author-uniqueid"], [data-e2e="browse-username"]') ||
                   (container.querySelector('a[href^="/@"], a[role="link"][href^="/"]')?.getAttribute("href") || "").replace(/\//g, "");
    const labels = [];
    if (hasAiLabel(video)) labels.push("AI-generated label");
    if (/#(ai|aiart|aigenerated|aivideo|madewithai)\b/i.test(caption)) labels.push("creator tagged #ai");

    if (!id) id = "h" + hash(caption + "|" + author + "|" + (video.currentSrc || "").slice(0, 200));
    return { key: platform + ":" + id, videoId: id, caption, author, labels };
  }

  // TikTok/IG show labels like "Contains AI-generated media" outside the caption, so search
  // up from the video until we'd start including the next post's video.
  function hasAiLabel(video) {
    const re = /AI[- ]generated|labell?ed as AI|Dijana AI|AI info|Made with AI/i;
    let el = video.parentElement;
    for (let i = 0; i < 20 && el && el !== document.body; i++, el = el.parentElement) {
      if (el.querySelectorAll("video").length > 1) break;
      if (re.test(el.innerText || "") || el.querySelector('[aria-label*="AI-generated" i]')) return true;
    }
    return false;
  }

  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }

  // ---------- grabbing frames ----------
  function scaledSize(w, h) {
    const s = Math.min(1, FRAME_MAX_SIDE / Math.max(w, h));
    return [Math.round(w * s), Math.round(h * s)];
  }

  function frameFromVideo(video) {
    const [w, h] = scaledSize(video.videoWidth, video.videoHeight);
    if (!w || !h) throw new Error("video not ready");
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    c.getContext("2d").drawImage(video, 0, 0, w, h);
    return c.toDataURL("image/jpeg", 0.8); // throws SecurityError if the video is cross-origin
  }

  async function frameFromScreenshot(video) {
    badge.hide();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const res = await send({ type: "capture" });
    badge.show();
    if (!res.ok) throw new Error(res.error || "capture failed");
    const img = new Image();
    img.src = res.data;
    await img.decode();
    const ratio = img.width / innerWidth;
    const r = video.getBoundingClientRect();
    const sx = Math.max(0, r.left) * ratio, sy = Math.max(0, r.top) * ratio;
    const sw = Math.min(r.width, innerWidth - Math.max(0, r.left)) * ratio;
    const sh = Math.min(r.height, innerHeight - Math.max(0, r.top)) * ratio;
    const [w, h] = scaledSize(sw, sh);
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    c.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, w, h);
    return c.toDataURL("image/jpeg", 0.8);
  }

  let canvasBlocked = false;
  async function grabFrame(video) {
    if (!canvasBlocked) {
      try { return frameFromVideo(video); }
      catch (e) { if (e.name === "SecurityError") canvasBlocked = true; else throw e; }
    }
    return frameFromScreenshot(video);
  }

  async function grabFrames(video, key) {
    const frames = [];
    for (let i = 0; i < FRAME_COUNT; i++) {
      if (!current || current.key !== key) return null; // user swiped away
      try { frames.push(await grabFrame(video)); } catch (e) { console.debug("[ScrollSafe] frame skipped:", e.message); }
      await new Promise((r) => setTimeout(r, FRAME_GAP_MS));
    }
    return frames;
  }

  // ---------- checking ----------
  async function check(info, video) {
    busy = true;
    try {
      badge.render({ status: "checking" }, info);
      const lookup = await send({ type: "lookup", platform, videoId: info.videoId, lang: settings.lang });
      if (lookup.ok && lookup.data) return finish(info, lookup.data);

      const frames = await grabFrames(video, info.key);
      if (frames === null) return;
      const res = await send({
        type: "analyze",
        payload: {
          platform, video_id: info.videoId, caption: info.caption, author: info.author,
          page_url: location.href, platform_labels: info.labels, frames, use_gemini: settings.useGemini, lang: settings.lang,
        },
      });
      if (!res.ok) {
        const i18n = Object.fromEntries(Object.entries(STR).map(([l, s]) => [l, { title: s.cantCheck, message: s.serverDown }]));
        return finish(info, { status: "error", ...i18n.en, i18n }, false);
      }
      finish(info, res.data);
    } finally {
      busy = false;
    }
  }

  function finish(info, verdict, remember = true) {
    if (remember && verdict.status !== "unknown") results.set(info.key, verdict);
    if (current && current.key === info.key) {
      current.state = "done";
      badge.render(verdict, info);
      if (settings.readAloud && verdict.status === "red") speak(verdict);
    }
  }

  function speak(v) {
    try {
      speechSynthesis.cancel();
      const lv = loc(v);
      const u = new SpeechSynthesisUtterance(`${lv.title}. ${lv.message}`);
      if (settings.lang === "ms") {
        u.lang = "ms-MY";
        const voice = speechSynthesis.getVoices().find((x) => x.lang.toLowerCase().startsWith("ms"));
        if (voice) u.voice = voice;  // otherwise the browser's default voice is used
      }
      speechSynthesis.speak(u);
    } catch {}
  }

  // ---------- the floating badge ----------
  const badge = (() => {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;z-index:2147483647;top:0;left:0;display:none;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { --ink: #0f172a; --sub: #475569; --muted: #94a3b8; --line: #e2e8f0; --blue: #2563eb; }
        .card { font: 15px/1.45 Inter, "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
                width: 330px; box-sizing: border-box; border-radius: 16px; padding: 14px 16px 14px 18px; color: var(--ink); background: #fff;
                position: relative; overflow: hidden; border: 1px solid var(--line); box-shadow: 0 10px 30px rgba(15,23,42,.28);
                --c: var(--muted); --t: #f1f5f9; }
        .card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--c); }
        .card.large { font-size: 19px; width: 410px; }
        .green { --c: #16a34a; --t: #f0fdf4; } .yellow { --c: #d97706; --t: #fffbeb; } .red { --c: #dc2626; --t: #fef2f2; }
        .head { display: flex; align-items: center; gap: 10px; }
        .ico { width: 32px; height: 32px; border-radius: 10px; background: var(--t); color: var(--c); display: grid; place-items: center; flex: none; }
        .large .ico { width: 40px; height: 40px; }
        .ico svg { width: 60%; height: 60%; }
        .title { font-size: 1.13em; font-weight: 700; letter-spacing: -.01em; line-height: 1.2; }
        .brand { font-size: .7em; font-weight: 600; color: var(--muted); letter-spacing: .06em; text-transform: uppercase; margin-top: 2px; }
        .x { margin-left: auto; align-self: flex-start; border: 0; background: none; color: var(--muted); padding: 4px; border-radius: 8px; cursor: pointer; display: grid; }
        .x:hover { background: #f1f5f9; color: var(--sub); }
        .x svg { width: 1.05em; height: 1.05em; }
        .msg { margin-top: 10px; font-size: 1em; line-height: 1.45; }
        .more { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 8px; }
        .reasons { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; font-size: .9em; color: var(--sub); }
        .reasons li { display: flex; gap: 8px; line-height: 1.4; }
        .reasons li::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--c); margin-top: .55em; flex: none; }
        .source { font-size: .88em; color: var(--blue); font-weight: 500; text-decoration: none; }
        .source:hover { text-decoration: underline; }
        .scores { display: grid; gap: 6px; }
        .score { display: grid; grid-template-columns: 5.2em 1fr 2.8em; align-items: center; gap: 8px; font-size: .8em; color: var(--sub); }
        .bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
        .bar i { display: block; height: 100%; border-radius: 3px; background: var(--c); }
        .score b { text-align: right; font-weight: 600; color: var(--ink); }
        .note { font-size: .78em; color: var(--muted); }
        .row { display: flex; gap: 6px; margin-top: 14px; flex-wrap: wrap; }
        .btn { font: inherit; display: inline-flex; align-items: center; gap: 6px; font-size: .9em; font-weight: 600; color: var(--ink); white-space: nowrap;
               border: 1px solid var(--line); background: #fff; border-radius: 10px; padding: 6px 10px; cursor: pointer; }
        .btn:hover { background: #f8fafc; }
        .btn:disabled { cursor: default; opacity: .7; }
        .btn svg { width: 1.05em; height: 1.05em; color: var(--sub); }
        .btn.dark { background: var(--ink); border-color: var(--ink); color: #fff; }
        .btn.dark:hover { background: #1e293b; }
        .btn.dark svg { color: #cbd5e1; }
        /* Small pill: made smaller, and while checking */
        .card.min { width: auto; padding: 6px 14px 6px 6px; border-radius: 999px; display: inline-flex; align-items: center; gap: 8px;
                    font-weight: 600; font-size: 14px; cursor: pointer; }
        .card.min.large { font-size: 18px; }
        .card.min::before { display: none; }
        .card.checking { cursor: default; }
        .dot { width: 22px; height: 22px; border-radius: 50%; background: var(--t); color: var(--c); display: grid; place-items: center; flex: none; }
        .dot svg { width: 14px; height: 14px; }
        .spin { width: 12px; height: 12px; border-radius: 50%; border: 2px solid #dbeafe; border-top-color: var(--blue); animation: spin .8s linear infinite; }
        .checking .dot { background: #eff6ff; }
        @keyframes spin { to { transform: rotate(360deg); } }
      </style>
      <div class="card" part="card"></div>`;
    const card = root.querySelector(".card");
    let minimized = false, expanded = false, last = null;
    (document.body || document.documentElement).appendChild(host);

    // Simple line icons instead of emoji, so they look the same on every computer.
    const svg = (d, w = 2) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
    const TICK = '<path d="m9 12 2 2 4-4"/>';
    const BANG = '<path d="M12 8v5M12 16.5h.01"/>';
    const ICON = {
      green: svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>' + TICK),
      yellow: svg('<circle cx="12" cy="12" r="10"/>' + BANG),
      red: svg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>'),
      grey: svg('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>'),
    };
    const PILL_BANG = svg('<path d="M12 5v9M12 19h.01"/>', 3.2);
    const PILL = { green: svg('<path d="m5 12 5 5 9-10"/>', 2.8), yellow: PILL_BANG, red: PILL_BANG, grey: PILL_BANG };
    const I_X = svg('<path d="M18 6 6 18M6 6l12 12"/>');
    const I_INFO = svg('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>');
    const I_SOUND = svg('<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>');
    const I_FLAG = svg('<path d="M4 22V4s1-1 4-1 5 2 8 2 4-1 4-1v11s-1 1-4 1-5-2-8-2-4 1-4 1"/>');
    const fmt = (x) => (x == null ? "n/a" : Math.round(x * 100) + "%");
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const scoreRow = (label, x) => x == null ? "" :
      `<div class="score"><span>${label}</span><div class="bar"><i style="width:${Math.round(x * 100)}%"></i></div><b>${fmt(x)}</b></div>`;

    function details(v) {
      const facts = (v.fact_checks || []).filter((f) => f.match === "match");
      const reasons = (v.details || []).filter((d) => d !== v.message);
      if (v.reports?.count) reasons.push(t("reports")(v.reports.count));
      const s = v.scores;
      return `<div class="more">
        ${reasons.length ? `<ul class="reasons">${reasons.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
        ${facts.map((f) => `<a class="source" href="${esc(f.url)}" target="_blank" rel="noopener">${esc(t("rated")(f.publisher, f.rating))}</a>`).join("")}
        ${s ? `<div class="scores">${scoreRow(t("deepfake"), s.deepfake)}${scoreRow(t("aiMade"), s.ai_generated)}</div>` : ""}
        ${v.signals_used && !v.signals_used.gemini ? `<div class="note">${t("helperOff")}</div>` : ""}
      </div>`;
    }

    function render(v, info) {
      last = { v, info };
      v = loc(v);  // text in the chosen language
      host.style.display = settings.enabled ? "block" : "none";
      const color = ["green", "yellow", "red"].includes(v.status) ? v.status : "grey";
      const checking = v.status === "checking";
      const small = minimized || checking;
      card.className = `card ${color} ${checking ? "checking" : ""} ${settings.largeText ? "large" : ""} ${small ? "min" : ""}`;
      if (small) {
        card.innerHTML = checking
          ? `<span class="dot"><span class="spin"></span></span>${t("checking")}`
          : `<span class="dot">${PILL[color]}</span>${esc(v.title || "ScrollSafe")}`;
        card.onclick = checking ? null : () => { minimized = false; render(v, info); };
        place();
        return;
      }
      card.onclick = null;
      card.innerHTML = `
        <div class="head">
          <div class="ico">${ICON[color]}</div>
          <div><div class="title">${esc(v.title)}</div><div class="brand">ScrollSafe</div></div>
          <button class="x" title="${t("smaller")}">${I_X}</button>
        </div>
        <div class="msg">${esc(v.message)}</div>
        ${expanded ? details(v) : ""}
        <div class="row">
          ${v.status !== "error" ? `<button class="btn dark" data-a="why">${I_INFO}${expanded ? t("hide") : t("why")}</button>` : ""}
          <button class="btn" data-a="speak">${I_SOUND}${t("read")}</button>
          ${v.status !== "error" ? `<button class="btn" data-a="report">${I_FLAG}${t("report")}</button>` : ""}
        </div>`;
      root.querySelector(".x")?.addEventListener("click", (e) => { e.stopPropagation(); minimized = true; render(v, info); });
      root.querySelectorAll("button[data-a]").forEach((b) => b.addEventListener("click", () => action(b.dataset.a, v, info, b)));
      place();
    }

    async function action(a, v, info, btn) {
      if (a === "why") { expanded = !expanded; render(v, info); }
      if (a === "speak") speak(v);
      if (a === "report") {
        btn.disabled = true;
        const res = await send({ type: "report", platform, videoId: info.videoId, label: "suspicious" });
        btn.innerHTML = `${I_FLAG}${res.ok ? t("reported") : t("reportFail")}`;
      }
    }

    function place() {
      const v = current?.video;
      if (!v || !v.isConnected) return;
      const r = v.getBoundingClientRect();
      const w = card.offsetWidth || 330;
      const left = Math.min(Math.max(8, r.left + 12), innerWidth - w - 8);
      const top = Math.min(Math.max(8, r.top + 12), innerHeight - 60);
      host.style.left = left + "px";
      host.style.top = top + "px";
    }

    return {
      render,
      place,
      hide: () => { host.style.visibility = "hidden"; },
      show: () => { host.style.visibility = "visible"; },
      clear: () => { host.style.display = "none"; expanded = false; },
      rerender: () => last && render(last.v, last.info),
    };
  })();

  // ---------- main loop ----------
  function tick() {
    if (!settings.enabled) { badge.clear(); return; }
    const video = activeVideo();
    if (!video) { badge.clear(); current = null; return; }
    const info = videoInfo(video);

    if (!current || current.key !== info.key) {
      current = { key: info.key, video, since: Date.now(), state: "waiting", info };
      const known = results.get(info.key);
      if (known) { current.state = "done"; badge.render(known, info); }
      else badge.clear();
      return;
    }
    current.video = video;
    badge.place();
    if (current.state === "waiting" && !busy && Date.now() - current.since >= DWELL_MS) {
      current.state = "checking";
      check(info, video);
    }
  }

  send({ type: "settings" }).then((res) => {
    if (res.ok) settings = res.data;
    setInterval(tick, TICK_MS);
    addEventListener("scroll", () => badge.place(), { passive: true, capture: true });
    addEventListener("resize", () => badge.place());
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync") return;
    for (const [k, { newValue }] of Object.entries(changes)) if (k in settings) settings[k] = newValue;
    badge.rerender();
  });
})();
