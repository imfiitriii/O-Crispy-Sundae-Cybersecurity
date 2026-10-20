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
  let settings = { enabled: true, largeText: false, readAloud: false, useGemini: true };
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
      const lookup = await send({ type: "lookup", platform, videoId: info.videoId });
      if (lookup.ok && lookup.data) return finish(info, lookup.data);

      const frames = await grabFrames(video, info.key);
      if (frames === null) return;
      const res = await send({
        type: "analyze",
        payload: {
          platform, video_id: info.videoId, caption: info.caption, author: info.author,
          page_url: location.href, platform_labels: info.labels, frames, use_gemini: settings.useGemini,
        },
      });
      if (!res.ok) return finish(info, { status: "error", title: "Couldn't check", message: "The checking server isn't reachable." }, false);
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
      speechSynthesis.speak(new SpeechSynthesisUtterance(`${v.title}. ${v.message}`));
    } catch {}
  }

  // ---------- the floating badge ----------
  const badge = (() => {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;z-index:2147483647;top:0;left:0;display:none;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        .card { font: 15px/1.35 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; width: 280px; border-radius: 14px;
                padding: 12px 14px; color: #111; background: #fff; box-shadow: 0 6px 24px rgba(0,0,0,.35); border-left: 8px solid #999; }
        .card.large { font-size: 19px; width: 340px; }
        .green { border-color: #1a7f37; } .yellow { border-color: #d4a106; } .red { border-color: #c62828; background: #fff5f5; }
        .checking, .error, .unknown { border-color: #888; }
        .head { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.1em; }
        .msg { margin-top: 4px; }
        .row { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
        button { font: inherit; font-size: .85em; border: 1px solid #ccc; background: #f6f6f6; border-radius: 8px; padding: 5px 9px; cursor: pointer; }
        button:hover { background: #ececec; }
        ul { margin: 8px 0 0; padding-left: 18px; font-size: .88em; color: #333; }
        a { color: #0b57d0; }
        .x { margin-left: auto; border: 0; background: none; font-size: 1.1em; padding: 0 4px; }
        .min { cursor: pointer; width: auto; padding: 8px 12px; }
      </style>
      <div class="card" part="card"></div>`;
    const card = root.querySelector(".card");
    let minimized = false, expanded = false, last = null;
    (document.body || document.documentElement).appendChild(host);

    const ICON = { green: "🟢", yellow: "🟡", red: "🔴", checking: "⏳", error: "⚪", unknown: "⚪" };
    const fmt = (x) => (x == null ? "n/a" : Math.round(x * 100) + "%");
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

    function render(v, info) {
      last = { v, info };
      host.style.display = settings.enabled ? "block" : "none";
      card.className = `card ${v.status} ${settings.largeText ? "large" : ""} ${minimized ? "min" : ""}`;
      if (minimized) {
        card.innerHTML = `<div class="head">${ICON[v.status] || "⚪"} ${esc(v.title || "ScrollSafe")}</div>`;
        card.onclick = () => { minimized = false; render(v, info); };
        return;
      }
      card.onclick = null;
      if (v.status === "checking") {
        card.innerHTML = `<div class="head">⏳ Checking this video…<button class="x" title="Hide">✕</button></div>`;
      } else {
        const facts = (v.fact_checks || []).filter((f) => f.match === "match");
        card.innerHTML = `
          <div class="head">${ICON[v.status] || "⚪"} ${esc(v.title)}<button class="x" title="Make smaller">✕</button></div>
          <div class="msg">${esc(v.message)}</div>
          ${expanded ? `<ul>${(v.details || []).filter((d) => d !== v.message).map((d) => `<li>${esc(d)}</li>`).join("")}
              ${facts.map((f) => `<li><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.publisher)}: ${esc(f.rating)}</a></li>`).join("")}
              ${v.reports?.count ? `<li>${v.reports.count} community report(s)</li>` : ""}</ul>
              ${v.scores ? `<div style="font-size:.75em;color:#777;margin-top:6px">Scores: deepfake ${fmt(v.scores.deepfake)}, AI ${fmt(v.scores.ai_generated)}${v.signals_used && !v.signals_used.gemini ? " · AI helper off" : ""}</div>` : ""}` : ""}
          <div class="row">
            ${v.status !== "error" ? `<button data-a="why">${expanded ? "Hide details" : "Why?"}</button>` : ""}
            <button data-a="speak">🔊 Read</button>
            ${v.status !== "error" ? `<button data-a="report">🚩 Report</button>` : ""}
          </div>`;
      }
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
        btn.textContent = res.ok ? "✓ Reported, thank you" : "Couldn't report";
      }
    }

    function place() {
      const v = current?.video;
      if (!v || !v.isConnected) return;
      const r = v.getBoundingClientRect();
      const w = card.offsetWidth || 280;
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
