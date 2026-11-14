(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("form"), input = $("url"), badge = $("badge"), clearBtn = $("clear");
  const goBtn = $("goBtn"), pasteBtn = $("pasteBtn"), err = $("err");
  const result = $("result"), thumb = $("thumb"), dur = $("dur");
  const logo = $("logo"), author = $("author"), title = $("title"), kindLine = $("kindLine");
  const grid = $("grid"), tiles = $("tiles"), pickCount = $("pickCount"), selAll = $("selAll");
  const chips = $("chips"), dl = $("dl"), dlLabel = $("dlLabel"), fill = $("fill");
  const recentWrap = $("recentWrap"), recentList = $("recent");

  const DOMAINS = {
    instagram: ["instagram.com", "instagr.am"], tiktok: ["tiktok.com"], facebook: ["facebook.com", "fb.watch", "fb.com"],
    x: ["x.com", "twitter.com", "t.co"], youtube: ["youtube.com", "youtu.be"], reddit: ["reddit.com", "redd.it"],
    pinterest: ["pinterest.com", "pin.it"], snapchat: ["snapchat.com"], threads: ["threads.net", "threads.com"],
    vimeo: ["vimeo.com"], soundcloud: ["soundcloud.com"], twitch: ["twitch.tv"], linkedin: ["linkedin.com"],
    tumblr: ["tumblr.com"], dailymotion: ["dailymotion.com", "dai.ly"], bilibili: ["bilibili.com", "b23.tv"],
  };
  const NAMES = { instagram: "Instagram", tiktok: "TikTok", facebook: "Facebook", x: "X", youtube: "YouTube", reddit: "Reddit",
    pinterest: "Pinterest", snapchat: "Snapchat", threads: "Threads", vimeo: "Vimeo", soundcloud: "SoundCloud", twitch: "Twitch",
    linkedin: "LinkedIn", tumblr: "Tumblr", dailymotion: "Dailymotion", bilibili: "Bilibili", web: "Web" };

  let data = null, quality = null, picked = new Set();

  const brand = (p, size) => p && p !== "web"
    ? `<span class="brand" style="--m:url(brands/${p}.svg)${size ? `;width:${size}px;height:${size}px` : ""}"></span>`
    : `<i class="ph ph-globe-simple"></i>`;

  const findUrl = (s) => (s.match(/https?:\/\/\S+/) || [""])[0];
  const platformOf = (u) => {
    try {
      const h = new URL(u).hostname.toLowerCase();
      for (const [p, ds] of Object.entries(DOMAINS)) if (ds.some((d) => h === d || h.endsWith("." + d))) return p;
      return "web";
    } catch { return null; }
  };
  const fmtSize = (b) => !b ? "" : b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? Math.round(b / 1e6) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
  const fmtDur = (s) => { if (!s) return ""; s = Math.round(s); const m = Math.floor(s / 60), r = String(s % 60).padStart(2, "0"); return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${r}` : `${m}:${r}`; };
  const proxied = (u) => u ? `api/thumb?u=${encodeURIComponent(u)}` : "";

  function setBadge() {
    const u = findUrl(input.value.trim());
    const p = u ? platformOf(u) : null;
    clearBtn.hidden = !input.value;
    if (p) { badge.innerHTML = brand(p, 20); badge.classList.add("on"); }
    else { badge.innerHTML = `<i class="ph ph-link-simple"></i>`; badge.classList.remove("on"); }
  }

  const dlErr = $("dlErr");
  function showErr(msg, where = err) {
    where.innerHTML = `<i class="ph ph-warning-circle"></i><span></span>`;
    where.querySelector("span").textContent = msg;
    where.hidden = false;
  }

  input.addEventListener("input", () => { err.hidden = true; setBadge(); });
  input.addEventListener("paste", () => setTimeout(() => { setBadge(); if (findUrl(input.value)) form.requestSubmit(); }, 0));
  clearBtn.addEventListener("click", () => { input.value = ""; setBadge(); result.hidden = true; input.focus(); });

  pasteBtn.addEventListener("click", async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (!t) return showErr("Your clipboard is empty. Copy the post's link first.");
      input.value = t.trim(); setBadge();
      if (findUrl(input.value)) form.requestSubmit(); else showErr("That doesn't look like a link. Copy the post's link and paste it again.");
    } catch { input.focus(); showErr("Long-press the box and tap Paste."); }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = findUrl(input.value.trim());
    if (!url) return showErr("That doesn't look like a link. Copy the post's link and paste it again.");
    err.hidden = true; dlErr.hidden = true; result.hidden = true;
    goBtn.disabled = true; goBtn.classList.add("loading"); goBtn.querySelector("span").textContent = "Finding";
    try {
      const r = await fetch("api/info", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "Couldn't read that link.");
      render(j);
    } catch (ex) {
      showErr(ex.message === "Failed to fetch" ? "No connection to the server. Check your internet and try again." : ex.message);
    } finally {
      goBtn.disabled = false; goBtn.classList.remove("loading"); goBtn.querySelector("span").textContent = "Find it";
    }
  });

  function render(j) {
    data = j;
    const items = j.items || [];
    const first = items[0] || {};
    thumb.style.backgroundImage = j.thumbnail ? `url("${proxied(j.thumbnail)}")` : "";
    const d = j.duration || (items.length === 1 && first.duration);
    dur.hidden = !d; dur.textContent = fmtDur(d);
    logo.innerHTML = brand(j.platform, 16);
    author.textContent = j.author ? `${NAMES[j.platform] || "Web"} · @${String(j.author).replace(/^@/, "")}` : (NAMES[j.platform] || "Web");
    title.textContent = j.title || "";
    title.hidden = !j.title;

    const vids = items.filter((i) => i.kind === "video").length, imgs = items.filter((i) => i.kind === "image").length;
    const parts = [];
    if (vids) parts.push(`${vids} video${vids > 1 ? "s" : ""}`);
    if (imgs) parts.push(`${imgs} image${imgs > 1 ? "s" : ""}`);
    if (!parts.length && items.some((i) => i.kind === "audio")) parts.push("Audio");
    kindLine.textContent = parts.join(" and ");

    picked = new Set(items.map((_, i) => i));
    if (items.length > 1) {
      grid.hidden = false;
      tiles.innerHTML = "";
      items.forEach((it, i) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "tile on"; b.setAttribute("aria-pressed", "true"); b.setAttribute("aria-label", `Item ${i + 1}`);
        if (it.thumbnail) b.style.backgroundImage = `url("${proxied(it.thumbnail)}")`;
        b.innerHTML = `<i class="ph-fill ph-check-circle"></i>${it.kind === "video" ? '<i class="ph-fill ph-play vid"></i>' : ""}`;
        b.addEventListener("click", () => {
          picked.has(i) ? picked.delete(i) : picked.add(i);
          b.classList.toggle("on", picked.has(i)); b.setAttribute("aria-pressed", picked.has(i));
          b.querySelector(".ph-check-circle").className = picked.has(i) ? "ph-fill ph-check-circle" : "ph ph-circle";
          updatePick();
        });
        tiles.appendChild(b);
      });
    } else grid.hidden = true;

    // quality chips: use the first video item's options (carousels share them), else the item's own
    const src = items.find((i) => i.kind === "video") || first;
    const qs = (src.qualities || []);
    chips.innerHTML = "";
    quality = qs.length ? (qs.find((q) => q.id === "1080") || qs[0]).id : "best";
    if (qs.length > 1 && items.length === 1) {
      qs.forEach((q) => {
        const c = document.createElement("button");
        c.type = "button"; c.className = "chip"; c.setAttribute("role", "radio");
        c.innerHTML = `${q.label}${q.size ? `<small>${fmtSize(q.size)}</small>` : ""}`;
        c.setAttribute("aria-checked", q.id === quality);
        c.addEventListener("click", () => {
          quality = q.id;
          chips.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-checked", x === c));
          updatePick();
        });
        chips.appendChild(c);
      });
    }
    resetDl();
    updatePick();
    result.hidden = false;
    result.style.animation = "none"; void result.offsetWidth; result.style.animation = "";
  }

  function updatePick() {
    const items = data.items || [];
    const n = picked.size;
    if (items.length > 1) {
      pickCount.textContent = `${n} of ${items.length} selected`;
      selAll.textContent = n === items.length ? "Select none" : "Select all";
    }
    dl.disabled = n === 0;
    if (dl.classList.contains("busy")) return;
    if (items.length > 1) { dlLabel.innerHTML = `<i class="ph ph-download-simple"></i>Download ${n} ${n === 1 ? "file" : "files"}${n > 1 ? " (zip)" : ""}`; return; }
    const q = (items[0]?.qualities || []).find((x) => x.id === quality);
    const what = quality === "audio" ? "MP3" : q && q.label !== "Original" && q.label !== "Best" ? q.label : (items[0]?.kind === "image" ? "image" : "");
    dlLabel.innerHTML = `<i class="ph ph-download-simple"></i>Download${what ? " " + what : ""}${q && q.size ? " · " + fmtSize(q.size) : ""}`;
  }

  selAll.addEventListener("click", () => {
    const all = picked.size !== data.items.length;
    picked = new Set(all ? data.items.map((_, i) => i) : []);
    tiles.querySelectorAll(".tile").forEach((t, i) => {
      t.classList.toggle("on", all); t.setAttribute("aria-pressed", all);
      t.querySelector(".ph-check-circle, .ph-circle").className = all ? "ph-fill ph-check-circle" : "ph ph-circle";
    });
    updatePick();
  });

  function resetDl() { dl.classList.remove("busy", "done"); fill.style.width = "0"; dl.disabled = false; }

  dl.addEventListener("click", async () => {
    if (!data || dl.classList.contains("busy")) return;
    const items = data.items || [];
    const sel = items.length > 1 ? [...picked].sort((a, b) => a - b).map((i) => i + 1).join(",") : "";
    const name = (data.title || data.author || "get-it").slice(0, 50);
    const q = new URLSearchParams({ url: data.url, quality: items.length > 1 ? (items.some(i=>i.kind==="video") ? "1080" : "best") : quality, engine: data.engine, items: sel, name });
    dl.classList.add("busy"); fill.style.width = "0"; dlErr.hidden = true;
    dlLabel.innerHTML = `<i class="ph ph-hourglass-medium"></i>Getting it ready`;
    try {
      const r = await fetch(`api/download?${q}`);
      if (!r.ok) { let m = "Download failed. Try again."; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
      const total = +r.headers.get("Content-Length") || 0;
      const cd = r.headers.get("Content-Disposition") || "";
      const fname = decodeURIComponent((cd.match(/filename\*=utf-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i) || [, "get-it"])[1]);
      const reader = r.body.getReader(); const chunks = []; let got = 0;
      for (;;) {
        const { done, value } = await reader.read(); if (done) break;
        chunks.push(value); got += value.length;
        if (total) { const pct = Math.round((got / total) * 100); fill.style.width = pct + "%"; dlLabel.innerHTML = `<i class="ph ph-download-simple"></i>Saving ${pct}%`; }
        else dlLabel.innerHTML = `<i class="ph ph-download-simple"></i>Saving ${fmtSize(got)}`;
      }
      const blob = new Blob(chunks, { type: r.headers.get("Content-Type") || "application/octet-stream" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = fname;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      dl.classList.remove("busy"); dl.classList.add("done"); fill.style.width = "0";
      dlLabel.innerHTML = `<i class="ph ph-check"></i>Saved`;
      addRecent({ url: data.url, platform: data.platform, title: data.title || data.author || fname, thumb: data.thumbnail, size: got, at: Date.now() });
      setTimeout(() => { dl.classList.remove("done"); updatePick(); }, 2400);
    } catch (ex) {
      dl.classList.remove("busy"); fill.style.width = "0"; updatePick();
      showErr(ex.message === "Failed to fetch" ? "Lost the connection mid-download. Try again." : ex.message, dlErr);
    }
  });

  // recent downloads live only on this device
  const KEY = "getit.recent";
  const loadRecent = () => { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch { return []; } };
  function addRecent(e) { const l = loadRecent().filter((x) => x.url !== e.url); l.unshift(e); localStorage.setItem(KEY, JSON.stringify(l.slice(0, 20))); drawRecent(); }
  function drawRecent() {
    const l = loadRecent(); recentWrap.hidden = !l.length; recentList.innerHTML = "";
    l.forEach((e) => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="rt"></div><div class="rm"><div class="rn"></div><div class="rs">${brand(e.platform, 13)}<span>${fmtSize(e.size)}</span></div></div><button class="iconbtn" aria-label="Open again"><i class="ph ph-arrow-counter-clockwise"></i></button>`;
      if (e.thumb) li.querySelector(".rt").style.backgroundImage = `url("${proxied(e.thumb)}")`;
      li.querySelector(".rn").textContent = e.title || e.url;
      li.querySelector("button").addEventListener("click", () => { input.value = e.url; setBadge(); window.scrollTo({ top: 0, behavior: "smooth" }); form.requestSubmit(); });
      recentList.appendChild(li);
    });
  }
  $("clearRecent").addEventListener("click", () => { localStorage.removeItem(KEY); drawRecent(); });

  // shared to the app from Android's share sheet (PWA share_target)
  const shared = new URLSearchParams(location.search);
  const incoming = findUrl([shared.get("url"), shared.get("text"), shared.get("title")].filter(Boolean).join(" "));
  drawRecent();
  if (incoming) { input.value = incoming; history.replaceState(null, "", location.pathname); setBadge(); form.requestSubmit(); }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
})();
