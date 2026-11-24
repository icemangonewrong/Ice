/* Ice for the web: a small Plex player for in-car browsers (CarTV) and any other browser.
 * Signs in with Plex (code at plex.tv/link), finds your server, browses it and plays through Plex's own
 * streaming (the video passes through untouched; only the sound is converted when the browser can't play it).
 * Subtitles off, English audio, 10-second skips, progress reported to Plex like the other Ice apps. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const store = {
  get(k, d) { try { const v = localStorage.getItem("ice." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ice." + k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem("ice." + k); } catch {} },
};

// ---------------------------------------------------------------- Plex
const CLIENT = store.get("client") || (() => { const id = "ice-web-" + Math.random().toString(36).slice(2) + Date.now().toString(36); store.set("client", id); return id; })();
const HEAD = {
  "Accept": "application/json",
  "X-Plex-Product": "Ice",
  "X-Plex-Version": "1.0",
  "X-Plex-Client-Identifier": CLIENT,
  "X-Plex-Platform": "Safari",
  "X-Plex-Device": "Car",
  "X-Plex-Device-Name": "Ice (web)",
};
let token = store.get("token", "");
let server = store.get("server", null);   // {name, base, token, uris}

function qs(o) { return Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&"); }

async function fetchJSON(url, opts = {}, ms = 15000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { ...opts, headers: { ...HEAD, ...(opts.headers || {}) }, signal: ctl.signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const txt = await r.text();
    return txt ? JSON.parse(txt) : {};
  } finally { clearTimeout(t); }
}

async function plexTv(path, opts = {}) {
  return fetchJSON("https://plex.tv/api/v2" + path, { ...opts, headers: { "X-Plex-Token": token, ...(opts.headers || {}) } });
}

/** A request to your server. If its address stops answering (VPN dropped…), switch to one that answers. */
async function api(path, params = {}, method = "GET", ms = 15000) {
  const go = (base) => fetchJSON(base + path + (path.includes("?") ? "&" : "?") + qs({ ...params, "X-Plex-Token": server.token }), { method }, ms);
  try { return await go(server.base); }
  catch (e) {
    const alt = await pickAddress(server.uris.filter((u) => u !== server.base), server.token);
    if (!alt) throw e;
    server.base = alt; store.set("server", server); showServer();
    return go(alt);
  }
}

/** Ask all addresses at once; first answer in preference order wins. */
async function pickAddress(uris, tok) {
  const ok = await Promise.all(uris.map((u) => fetchJSON(u + "/identity?X-Plex-Token=" + encodeURIComponent(tok), {}, 4000).then(() => true, () => false)));
  const i = ok.indexOf(true);
  return i >= 0 ? uris[i] : null;
}

// Libraries you've switched off in Settings (old/legacy ones) stay out of Home and Libraries.
function hiddenLibs() { return store.get("hiddenLibs", []); }
function shownLibs(dirs) { const h = hiddenLibs(); return dirs.filter((d) => (d.type === "movie" || d.type === "show") && !h.includes(String(d.key))); }

// Text size: car screens are wide, so the page starts bigger there; Settings has buttons to change it.
const SIZES = [["Small", 1], ["Medium", 1.25], ["Large", 1.5], ["Huge", 1.85]];
function applySize() {
  const z = store.get("zoom", innerWidth >= 900 ? 1.5 : 1);
  document.documentElement.style.zoom = z;
}
applySize();

function img(path, w, h) {
  if (!path || !server) return "";
  return server.base + "/photo/:/transcode?" + qs({ width: w, height: h, minSize: 1, upscale: 1, url: path, "X-Plex-Token": server.token });
}
const items = (d) => (d && d.MediaContainer && (d.MediaContainer.Metadata || [])) || [];

// ---------------------------------------------------------------- helpers
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function clock(sec) { sec = Math.max(0, Math.floor(sec)); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0"); }
function niceDate(s) { if (!s) return ""; const d = new Date(s + "T12:00:00"); return isNaN(d) ? s : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
function epCode(it) { return it.parentIndex != null && it.index != null ? `S${it.parentIndex} · E${it.index}` : ""; }
function spinner() { return el('<div class="spin"></div>'); }
function msg(text) { return el(`<div class="box center"><p>${esc(text)}</p></div>`); }
function setTitle(back) { $("#back").hidden = !back; }
function showServer() { $("#server").textContent = server ? server.name : ""; }

function card(it, wide = false) {
  const isEp = it.type === "episode";
  const title = isEp ? (it.grandparentTitle || it.title) : it.title;
  const sub = isEp ? [epCode(it), it.title].filter(Boolean).join(" · ") : [it.year, it.contentRating].filter(Boolean).join(" · ");
  const poster = wide ? img(it.art || it.thumb, 480, 270) : img(isEp ? (it.grandparentThumb || it.parentThumb || it.thumb) : it.thumb, 280, 420);
  const pct = it.viewOffset && it.duration ? Math.min(100, it.viewOffset / it.duration * 100) : 0;
  const watched = (it.viewCount && !it.viewOffset) || (it.type === "show" && it.leafCount && it.viewedLeafCount === it.leafCount);
  const c = el(`<a class="card${wide ? " wide" : ""}" href="#/item/${it.ratingKey}">
    <div class="img">${poster ? `<img loading="lazy" src="${poster}" alt="">` : ""}
      ${pct ? `<div class="prog-bg"></div><div class="prog" style="width:${pct}%"></div>` : ""}
      ${watched ? '<div class="check">✓</div>' : ""}</div>
    <div class="t">${esc(title)}</div><div class="s">${esc(sub)}</div></a>`);
  if (isEp || it.type === "movie") {
    // Continue Watching / episodes: tap plays straight away.
    if (pct) c.addEventListener("click", (e) => { e.preventDefault(); play(it.ratingKey); });
  }
  return c;
}

function rowSection(title, list, link, wide) {
  const s = el(`<section><h2>${link ? `<a href="${link}">${esc(title)}</a>` : esc(title)}</h2><div class="row"></div></section>`);
  const r = $(".row", s);
  list.forEach((it) => r.appendChild(card(it, wide)));
  return s;
}

// ---------------------------------------------------------------- sign-in & server
async function signIn() {
  setTitle(false);
  view.replaceChildren(el(`<div class="box center"><h1>Sign in to Plex</h1><p class="muted">Getting a code…</p></div>`));
  let pin;
  try { pin = await fetchJSON("https://plex.tv/api/v2/pins?strong=false", { method: "POST" }); }
  catch { view.replaceChildren(msg("Can't reach plex.tv. Check the connection and reload.")); return; }
  view.replaceChildren(el(`<div class="box center"><h1>Sign in to Plex</h1>
    <p>On your phone or computer, go to</p><p><b>plex.tv/link</b></p><p>and enter</p>
    <p class="code">${esc(pin.code)}</p><p class="muted">This page carries on by itself once you've entered it.</p></div>`));
  for (let i = 0; i < 300; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    if (!location.hash.startsWith("#/signin")) return;
    try {
      const p = await fetchJSON(`https://plex.tv/api/v2/pins/${pin.id}`);
      if (p.authToken) { token = p.authToken; store.set("token", token); return chooseServer(); }
    } catch {}
  }
  view.replaceChildren(msg("The code expired. Reload to get a new one."));
}

async function chooseServer() {
  view.replaceChildren(spinner());
  let res;
  try { res = await plexTv("/resources?includeHttps=1&includeRelay=1"); }
  catch { view.replaceChildren(msg("Couldn't get your servers from plex.tv.")); return; }
  const servers = res.filter((r) => (r.provides || "").includes("server"));
  if (!servers.length) { view.replaceChildren(msg("No Plex servers on this account.")); return; }
  const pickOne = async (r) => {
    view.replaceChildren(spinner());
    // Preference: local, then remote, then Plex relay (slowest).
    const conns = (r.connections || []).slice().sort((a, b) => (a.relay - b.relay) || (b.local - a.local));
    const uris = [...new Set(conns.map((c) => c.uri))];
    const base = await pickAddress(uris, r.accessToken);
    if (!base) { view.replaceChildren(msg(`Can't reach ${r.name} right now. Is WireGuard on?`)); return; }
    server = { name: r.name, base, token: r.accessToken, uris };
    store.set("server", server); showServer();
    location.hash = "#/home";
  };
  if (servers.length === 1) return pickOne(servers[0]);
  const l = el(`<div><h2>Pick your server</h2><div class="list"></div></div>`);
  servers.forEach((r) => { const b = el(`<button>${esc(r.name)}</button>`); b.onclick = () => pickOne(r); $(".list", l).appendChild(b); });
  view.replaceChildren(l);
}

// ---------------------------------------------------------------- pages
async function home() {
  setTitle(false);
  view.replaceChildren(spinner());
  const out = el("<div></div>");
  try {
    const [cw, secs] = await Promise.all([
      api("/hubs/home/continueWatching").catch(() => api("/library/onDeck")),
      api("/library/sections"),
    ]);
    const cont = cw.MediaContainer.Hub ? (cw.MediaContainer.Hub[0] || {}).Metadata || [] : items(cw);
    if (cont.length) out.appendChild(rowSection("Continue Watching", cont, null, false));
    const libs = shownLibs(secs.MediaContainer.Directory || []);
    const rows = await Promise.all(libs.map((d) => api(`/library/sections/${d.key}/recentlyAdded`, { "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 20 }).then(items, () => [])));
    libs.forEach((d, i) => { if (rows[i].length) out.appendChild(rowSection(`Recently Added · ${d.title}`, rows[i], `#/lib/${d.key}/${d.type}/recent`)); });
    view.replaceChildren(out);
  } catch (e) {
    view.replaceChildren(msg("Couldn't reach your Plex server. If you're away from home, check that WireGuard is on."));
  }
}

async function libraries() {
  setTitle(false);
  view.replaceChildren(spinner());
  try {
    const d = await api("/library/sections");
    const l = el(`<div><h2>Libraries</h2><div class="list"></div></div>`);
    shownLibs(d.MediaContainer.Directory || [])
      .forEach((x) => $(".list", l).appendChild(el(`<a href="#/lib/${x.key}/${x.type}">${esc(x.title)}</a>`)));
    view.replaceChildren(l);
  } catch { view.replaceChildren(msg("Couldn't load your libraries.")); }
}

const SORTS = [["Title", "titleSort"], ["Recently added", "addedAt:desc"], ["Release date", "originallyAvailableAt:desc"],
  ["Newest episode", "episode.addedAt:desc", "show"], ["Recently watched", "lastViewedAt:desc"], ["Audience rating", "audienceRating:desc"]];

async function library(key, type, mode) {
  setTitle(true);
  let sort = store.get("sort." + key, mode === "recent" ? (type === "show" ? "episode.addedAt:desc" : "addedAt:desc") : "titleSort");
  if (mode === "recent") sort = type === "show" ? "episode.addedAt:desc" : "addedAt:desc";
  let unwatched = store.get("unw." + key, false);
  const page = el(`<div><div class="bar"><select></select><label class="pill"><input type="checkbox"> Unwatched</label></div><div class="grid"></div><div class="more"></div></div>`);
  const sel = $("select", page), grid = $(".grid", page), more = $(".more", page), chk = $("input", page);
  SORTS.filter((s) => !s[2] || s[2] === type).forEach(([n, v]) => sel.appendChild(el(`<option value="${v}">${n}</option>`)));
  sel.value = sort; chk.checked = unwatched;
  let start = 0, total = Infinity, busy = false, gen = 0;
  const load = async () => {
    if (busy || start >= total) return;
    busy = true; const g = gen;
    more.replaceChildren(spinner());
    try {
      const d = await api(`/library/sections/${key}/all`, { sort, type: type === "show" ? 2 : 1, unwatched: unwatched ? 1 : "", "X-Plex-Container-Start": start, "X-Plex-Container-Size": 60 });
      if (g !== gen) return;
      const list = items(d);
      total = d.MediaContainer.totalSize ?? (start + list.length);
      start += list.length;
      list.forEach((it) => grid.appendChild(card(it)));
      if (!list.length) total = start;
    } catch { more.replaceChildren(msg("Couldn't load more.")); busy = false; return; }
    busy = false; more.replaceChildren();
    if (start < total && more.getBoundingClientRect().top < innerHeight + 600) load();
  };
  const reset = () => { gen++; start = 0; total = Infinity; busy = false; grid.replaceChildren(); load(); };
  sel.onchange = () => { sort = sel.value; store.set("sort." + key, sort); reset(); };
  chk.onchange = () => { unwatched = chk.checked; store.set("unw." + key, unwatched); reset(); };
  new IntersectionObserver((e) => { if (e[0].isIntersecting) load(); }, { rootMargin: "600px" }).observe(more);
  view.replaceChildren(page);
  load();
}

function search() {
  setTitle(false);
  const page = el(`<div><div class="bar"><input type="search" placeholder="Movies & shows" autocomplete="off"></div><div class="grid"></div></div>`);
  const box = $("input", page), grid = $(".grid", page);
  box.value = store.get("lastSearch", "");
  let t, gen = 0;
  const run = async () => {
    const q = box.value.trim(); store.set("lastSearch", q);
    const g = ++gen;
    if (!q) { grid.replaceChildren(); return; }
    grid.replaceChildren(spinner());
    try {
      const d = await api("/hubs/search", { query: q, limit: 30 });
      if (g !== gen) return;
      const found = [];
      (d.MediaContainer.Hub || []).forEach((h) => (h.Metadata || []).forEach((m) => { if (["movie", "show", "episode"].includes(m.type)) found.push(m); }));
      found.sort((a, b) => (b.originallyAvailableAt || b.year || "").toString().localeCompare((a.originallyAvailableAt || a.year || "").toString()));
      grid.replaceChildren(...(found.length ? found.map((m) => card(m)) : [msg("Nothing found.")]));
    } catch { grid.replaceChildren(msg("Search failed.")); }
  };
  box.oninput = () => { clearTimeout(t); t = setTimeout(run, 350); };
  view.replaceChildren(page);
  if (box.value) run();
}

function settings() {
  setTitle(false);
  const page = el(`<div>
    <div class="box"><p><b>Server</b></p><p class="muted srv"></p><p class="muted addr"></p>
      <p><button class="pill" id="sw">Switch server</button></p></div>
    <div class="box"><p><b>Text size</b></p><div class="sizes bar" style="padding:0"></div></div>
    <div class="box"><p><b>Libraries</b></p><p class="muted">Tick the ones to show on Home and in Libraries.</p><div class="libs"></div></div>
    <div class="box"><p><b>Playback</b></p><p class="muted">Subtitles start off and audio in English. The video plays as-is; only the sound is converted when this browser can't play it.</p></div>
    <div class="box"><p><button class="pill" id="out">Sign out</button></p></div></div>`);
  $(".srv", page).textContent = server ? server.name : "";
  $(".addr", page).textContent = server ? server.base.replace(/^https?:\/\//, "").replace(/\..*plex\.direct/, " (plex.direct)") : "";
  const cur = store.get("zoom", innerWidth >= 900 ? 1.5 : 1);
  SIZES.forEach(([n, z]) => {
    const b = el(`<button class="pill">${n}</button>`);
    if (Math.abs(z - cur) < 0.01) b.style.cssText = "background:var(--ice);color:#000";
    b.onclick = () => { store.set("zoom", z); applySize(); settings(); };
    $(".sizes", page).appendChild(b);
  });
  api("/library/sections").then((d) => {
    const hidden = hiddenLibs();
    (d.MediaContainer.Directory || []).filter((x) => x.type === "movie" || x.type === "show").forEach((x) => {
      const row = el(`<label class="check-row"><input type="checkbox"> <span></span></label>`);
      $("span", row).textContent = x.title;
      const cb = $("input", row);
      cb.checked = !hidden.includes(String(x.key));
      cb.onchange = () => {
        const h = hiddenLibs().filter((k) => k !== String(x.key));
        if (!cb.checked) h.push(String(x.key));
        store.set("hiddenLibs", h);
      };
      $(".libs", page).appendChild(row);
    });
  }).catch(() => {});
  $("#sw", page).onclick = () => { location.hash = "#/signin"; chooseServer(); };
  $("#out", page).onclick = () => { store.del("token"); store.del("server"); token = ""; server = null; showServer(); location.hash = "#/signin"; };
  view.replaceChildren(page);
}

async function item(key) {
  setTitle(true);
  view.replaceChildren(spinner());
  let it;
  try { it = items(await api(`/library/metadata/${key}`))[0]; } catch {}
  if (!it) { view.replaceChildren(msg("Couldn't load this.")); return; }
  if (it.type === "episode") return play(key);
  const page = el(`<div>
    <div class="hero">${it.art ? `<img src="${img(it.art, 1280, 720)}" alt="">` : ""}</div>
    <div class="detail"><div class="poster">${it.thumb ? `<img src="${img(it.thumb, 300, 450)}" alt="">` : ""}</div>
      <div class="info"><h1></h1><div class="meta"></div><div class="badges"></div></div></div>
    <div class="actions"></div><p class="summary"></p><div class="kids"></div></div>`);
  $("h1", page).textContent = it.title;
  const bits = [it.year, it.type === "show" ? (it.childCount ? `${it.childCount} season${it.childCount > 1 ? "s" : ""}` : "") : it.duration ? Math.round(it.duration / 60000) + " min" : "",
    (it.Genre || []).slice(0, 2).map((g) => g.tag).join(", ")].filter(Boolean);
  $(".meta", page).textContent = bits.join(" · ");
  if (it.contentRating) $(".badges", page).appendChild(el(`<span class="badge r">${esc(it.contentRating)}</span>`));
  if (it.audienceRating) $(".badges", page).appendChild(el(`<span class="badge imdb">${Number(it.audienceRating).toFixed(1)}</span>`));
  $(".summary", page).textContent = it.summary || "";
  const act = $(".actions", page);
  if (it.type === "movie") {
    const off = it.viewOffset ? ` from ${clock(it.viewOffset / 1000)}` : "";
    const b = el(`<button class="btn main">▶ ${it.viewOffset ? "Resume" + off : "Play"}</button>`);
    b.onclick = () => play(it.ratingKey);
    act.appendChild(b);
    if (it.viewOffset) { const r = el(`<button class="btn alt">From the start</button>`); r.onclick = () => play(it.ratingKey, 0); act.appendChild(r); }
    const w = el(`<button class="btn alt">${it.viewCount ? "Mark unwatched" : "Mark watched"}</button>`);
    w.onclick = async () => { await api(it.viewCount ? "/:/unscrobble" : "/:/scrobble", { key: it.ratingKey, identifier: "com.plexapp.plugins.library" }).catch(() => {}); item(key); };
    act.appendChild(w);
  } else if (it.type === "show") {
    const nxt = el(`<button class="btn main">▶ Play next episode</button>`);
    nxt.onclick = async () => {
      const eps = items(await api(`/library/metadata/${key}/allLeaves`).catch(() => ({})));
      const e = eps.find((x) => x.viewOffset) || eps.find((x) => !x.viewCount) || eps[0];
      if (e) play(e.ratingKey);
    };
    act.appendChild(nxt);
    const seasons = items(await api(`/library/metadata/${key}/children`).catch(() => ({})));
    const kids = $(".kids", page);
    seasons.forEach((s) => kids.appendChild(el(`<a class="list" href="#/season/${s.ratingKey}"><span style="display:block;padding:14px 16px;border-bottom:1px solid #1c1c22;font-size:17px">${esc(s.title)} <span class="muted">· ${s.leafCount || 0} ep${s.viewedLeafCount === s.leafCount && s.leafCount ? " · watched" : ""}</span></span></a>`)));
  }
  view.replaceChildren(page);
}

async function season(key) {
  setTitle(true);
  view.replaceChildren(spinner());
  try {
    const [sd, ed] = await Promise.all([api(`/library/metadata/${key}`), api(`/library/metadata/${key}/children`)]);
    const s = items(sd)[0] || {};
    const page = el(`<div><h2></h2></div>`);
    $("h2", page).textContent = `${s.parentTitle || ""} · ${s.title || ""}`;
    items(ed).forEach((e) => {
      const pct = e.viewOffset && e.duration ? e.viewOffset / e.duration * 100 : 0;
      const row = el(`<a class="ep" href="#/play/${e.ratingKey}"><div class="img">${e.thumb ? `<img loading="lazy" src="${img(e.thumb, 320, 180)}" alt="">` : ""}
        ${pct ? `<div class="prog-bg"></div><div class="prog" style="width:${pct}%"></div>` : ""}${e.viewCount && !e.viewOffset ? '<div class="check">✓</div>' : ""}</div>
        <div><div class="t"></div><div class="d"></div><div class="x"></div></div></a>`);
      $(".t", row).textContent = `${e.index}. ${e.title}`;
      $(".d", row).textContent = [niceDate(e.originallyAvailableAt), e.duration ? Math.round(e.duration / 60000) + " min" : ""].filter(Boolean).join(" · ");
      $(".x", row).textContent = e.summary || "";
      page.appendChild(row);
    });
    view.replaceChildren(page);
  } catch { view.replaceChildren(msg("Couldn't load this season.")); }
}

// ---------------------------------------------------------------- player
const P = { item: null, base: 0, dur: 0, session: "", timer: null, idle: null, next: null, scrobbled: false, lastReport: 0, playing: false };
const video = $("#video"), player = $("#player");

function now() { return P.base + (video.currentTime || 0); }

function streamURL(it, at) {
  P.session = "ice-" + Math.random().toString(36).slice(2);
  // Direct-stream the video (no video conversion on the server), convert only the sound if needed, no subtitles.
  return server.base + "/video/:/transcode/universal/start.m3u8?" + qs({
    path: "/library/metadata/" + it.ratingKey, mediaIndex: 0, partIndex: 0, protocol: "hls", offset: Math.floor(at),
    fastSeek: 1, directPlay: 0, directStream: 1, directStreamAudio: 0, subtitles: "none", audioBoost: 100,
    videoQuality: 100, maxVideoBitrate: 200000, videoResolution: "3840x2160", location: "lan",
    session: P.session, "X-Plex-Session-Identifier": P.session, "X-Plex-Client-Identifier": CLIENT,
    "X-Plex-Product": "Ice", "X-Plex-Platform": "Safari", "X-Plex-Device": "Car", "X-Plex-Token": server.token,
    "X-Plex-Client-Profile-Extra": "add-transcode-target-codec(type=videoProfile&context=streaming&protocol=hls&videoCodec=hevc)+add-direct-play-profile(type=videoProfile&container=mp4&videoCodec=h264,hevc&audioCodec=aac)",
  });
}

async function englishAudio(it) {
  // Start in English: pick the English track if the file has one and something else is selected.
  const part = it.Media && it.Media[0] && it.Media[0].Part && it.Media[0].Part[0];
  if (!part || !part.Stream) return;
  const audio = part.Stream.filter((s) => s.streamType === 2);
  const eng = audio.find((s) => /^(en|eng)$/i.test(s.languageCode || "") || /english/i.test(s.language || ""));
  const sel = audio.find((s) => s.selected);
  if (eng && sel && eng.id !== sel.id) await api(`/library/parts/${part.id}`, { audioStreamID: eng.id, allParts: 1 }, "PUT").catch(() => {});
  const subOn = part.Stream.some((s) => s.streamType === 3 && s.selected);
  if (subOn) await api(`/library/parts/${part.id}`, { subtitleStreamID: 0, allParts: 1 }, "PUT").catch(() => {});
}

async function play(key, from) {
  showPlayer(true);
  setMsg("");
  $("#spinner").hidden = false;
  let it;
  try { it = items(await api(`/library/metadata/${key}`))[0]; } catch {}
  if (!it) { setMsg("Couldn't load this video."); return; }
  P.item = it; P.dur = (it.duration || 0) / 1000; P.scrobbled = false; P.next = null;
  $("#ptitle").textContent = it.type === "episode" ? `${it.grandparentTitle} · ${epCode(it)} · ${it.title}` : it.title;
  if (location.hash.startsWith("#/play/")) history.replaceState(null, "", "#/item/" + (it.grandparentRatingKey || key));
  await englishAudio(it);
  const at = from != null ? from : (it.viewOffset || 0) / 1000;
  start(at);
  findNext(it);
}

function start(at) {
  P.base = at;
  video.src = streamURL(P.item, at);
  video.play().catch(() => { setPlayIcon(); });
  report("playing", true);
}

async function findNext(it) {
  if (it.type !== "episode" || !it.grandparentRatingKey) return;
  const eps = items(await api(`/library/metadata/${it.grandparentRatingKey}/allLeaves`).catch(() => ({})));
  const i = eps.findIndex((e) => String(e.ratingKey) === String(it.ratingKey));
  P.next = i >= 0 && i + 1 < eps.length ? eps[i + 1] : null;
  $("#pnext").hidden = !P.next;
}

function stopSession() {
  if (P.session && server) fetch(server.base + "/video/:/transcode/universal/stop?" + qs({ session: P.session, "X-Plex-Token": server.token }), { headers: HEAD }).catch(() => {});
}

function closePlayer() {
  if (!P.item) return showPlayer(false);
  report("stopped", true);
  stopSession();
  video.pause(); video.removeAttribute("src"); video.load();
  P.item = null;
  showPlayer(false);
  route();
}

function report(state, force) {
  if (!P.item || !server) return;
  const t = Date.now();
  if (!force && t - P.lastReport < 10000) return;
  P.lastReport = t;
  const ms = Math.round(now() * 1000);
  api("/:/timeline", { ratingKey: P.item.ratingKey, key: "/library/metadata/" + P.item.ratingKey, state, time: ms, duration: Math.round(P.dur * 1000), "X-Plex-Session-Identifier": P.session }).catch(() => {});
  if (!P.scrobbled && P.dur && now() / P.dur >= 0.9) {
    P.scrobbled = true;
    api("/:/scrobble", { key: P.item.ratingKey, identifier: "com.plexapp.plugins.library" }).catch(() => {});
  }
}

function seekTo(t) {
  t = Math.max(0, Math.min(t, P.dur ? P.dur - 2 : t));
  const rel = t - P.base;
  const s = video.seekable;
  if (rel >= 0 && s.length && rel <= s.end(s.length - 1) - 1) video.currentTime = rel;
  else { stopSession(); start(t); }     // outside what the server has made so far: restart the stream there
  poke();
}

function setPlayIcon() { $("#pplay").textContent = video.paused ? "▶" : "❚❚"; }
function setMsg(t) { const m = $("#pmsg"); m.textContent = t; m.hidden = !t; }
function showPlayer(on) {
  player.hidden = !on;
  document.body.style.overflow = on ? "hidden" : "";
  if (on) poke();
}
function poke() {
  player.classList.remove("idle");
  clearTimeout(P.idle);
  P.idle = setTimeout(() => { if (!video.paused) player.classList.add("idle"); }, 3500);
}
function tick() {
  const t = now();
  $("#pcur").textContent = clock(t);
  $("#pleft").textContent = "-" + clock(Math.max(0, P.dur - t));
  if (!P.dragging && P.dur) $("#pbar").value = Math.round(t / P.dur * 1000);
}

video.addEventListener("timeupdate", () => { tick(); report(video.paused ? "paused" : "playing"); });
video.addEventListener("waiting", () => { $("#spinner").hidden = false; });
video.addEventListener("playing", () => { $("#spinner").hidden = true; setMsg(""); setPlayIcon(); poke(); });
video.addEventListener("pause", () => { setPlayIcon(); report("paused", true); poke(); });
video.addEventListener("play", setPlayIcon);
video.addEventListener("ended", () => {
  if (P.dur && now() < P.dur - 20) { start(now()); return; }   // stream ended early (connection): pick up where it stopped
  report("stopped", true);
  if (P.next) play(P.next.ratingKey, 0); else closePlayer();
});
video.addEventListener("error", () => {
  if (!P.item) return;
  setMsg("Connection hiccup — picking up where it stopped…");
  const at = now();
  setTimeout(() => { if (P.item) { stopSession(); start(at); } }, 3000);
});
// Stalled while it should be playing (connection dropped and came back): restart at the same spot.
setInterval(() => {
  if (!P.item || video.paused) { P.lastT = null; return; }
  const t = now();
  if (P.lastT != null && Math.abs(t - P.lastT) < 0.05) {
    P.stuck = (P.stuck || 0) + 1;
    if (P.stuck === 15) { setMsg("Reconnecting…"); stopSession(); start(t); }
    if (P.stuck > 45) P.stuck = 0;
  } else { P.stuck = 0; }
  P.lastT = t;
}, 1000);

$("#pplay").onclick = (e) => { e.stopPropagation(); video.paused ? video.play() : video.pause(); poke(); };
$("#pback").onclick = (e) => { e.stopPropagation(); seekTo(now() - 10); };
$("#pfwd").onclick = (e) => { e.stopPropagation(); seekTo(now() + 10); };
$("#pclose").onclick = (e) => { e.stopPropagation(); closePlayer(); };
$("#pnext").onclick = (e) => { e.stopPropagation(); if (P.next) { report("stopped", true); stopSession(); play(P.next.ratingKey, 0); } };
$("#pbar").addEventListener("input", () => { P.dragging = true; $("#pcur").textContent = clock($("#pbar").value / 1000 * P.dur); poke(); });
$("#pbar").addEventListener("change", () => { P.dragging = false; seekTo($("#pbar").value / 1000 * P.dur); });
player.addEventListener("click", (e) => {
  if (e.target.closest("button, input")) return;
  if (player.classList.contains("idle")) poke(); else { player.classList.add("idle"); }
});
document.addEventListener("keydown", (e) => {
  if (player.hidden) return;
  if (e.key === " ") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  else if (e.key === "ArrowLeft") seekTo(now() - 10);
  else if (e.key === "ArrowRight") seekTo(now() + 10);
  else if (e.key === "Escape") closePlayer();
});
addEventListener("pagehide", () => { if (P.item) { report("stopped", true); stopSession(); } });

// ---------------------------------------------------------------- routing
const history_ = [];
$("#back").onclick = () => history.back();

function route() {
  const h = location.hash || "#/home";
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", h.startsWith("#/" + a.dataset.tab) ||
    (a.dataset.tab === "libraries" && /^#\/(lib|item|season)\//.test(h))));
  if (!token) { if (!h.startsWith("#/signin")) location.hash = "#/signin"; return signIn(); }
  if (!server) return chooseServer();
  showServer();
  scrollTo(0, 0);
  const [, page, a, b, c] = h.split("/");
  if (page === "home") return home();
  if (page === "libraries") return libraries();
  if (page === "lib") return library(a, b, c);
  if (page === "search") return search();
  if (page === "settings") return settings();
  if (page === "item") return item(a);
  if (page === "season") return season(a);
  if (page === "play") return play(a);
  if (page === "signin") return token ? chooseServer() : signIn();
  location.hash = "#/home";
}
addEventListener("hashchange", () => { if (!player.hidden && P.item && !location.hash.startsWith("#/play")) closePlayerQuietly(); route(); });
function closePlayerQuietly() { report("stopped", true); stopSession(); video.pause(); video.removeAttribute("src"); video.load(); P.item = null; showPlayer(false); }
route();
