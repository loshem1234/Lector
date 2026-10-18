/* Lector — read-along player for Gemini voices */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const RATES = [0.8, 1, 1.15, 1.25, 1.5, 1.75, 2];
const SKIP_KINDS = new Set(["footnote", "header", "pagenum"]);
const LABELS = { footnote: "Note · not read", header: "Running head", pagenum: "Page number" };

let ME = {};
let toastTimer;
function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers["Content-Type"] = "application/json";
    o.method = o.method || "POST";
    delete o.json;
  }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") {
    show("login");
    throw new Error("Not signed in");
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

function fmtSize(b) {
  if (b < 1024 * 1024) return Math.max(1, Math.round(b / 1024)) + " KB";
  if (b < 1024 ** 3) return (b / 1024 / 1024).toFixed(1) + " MB";
  return (b / 1024 ** 3).toFixed(2) + " GB";
}
function fmtDur(s) {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

/* ------------------------------------------------------------------ routing */

function show(name) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $("#v-" + name).classList.remove("hidden");
}

async function route() {
  const h = location.hash || "#/library";
  if (!R.empty() && !h.startsWith("#/read/")) R.close();
  try {
    ME = await api("/api/me");
  } catch (_) { ME = {}; }
  if (ME.password_set && !ME.signed_in) return show("login");
  $("#mock-banner").classList.toggle("hidden", !ME.mock);
  $("#pw-banner").classList.toggle("hidden", !!ME.password_set);
  if (h.startsWith("#/new")) { show("new"); resetNew(); }
  else if (h.startsWith("#/read/")) { show("reader"); R.open(h.slice(7)); }
  else { show("library"); loadLibrary(); }
}
window.addEventListener("hashchange", route);
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (g) location.hash = "#/" + g.dataset.go;
});

/* ------------------------------------------------------------------ login */

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    await api("/api/login", { json: { password: $("#login-pw").value } });
    $("#login-pw").value = "";
    route();
  } catch (err) {
    $("#login-err").textContent = err.message;
  }
});
$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  route();
});

/* ------------------------------------------------------------------ library */

$("#btn-new").addEventListener("click", () => (location.hash = "#/new"));

async function loadLibrary() {
  let data;
  try { data = await api("/api/docs"); } catch (e) { return; }
  const list = $("#lib-list");
  list.innerHTML = "";
  $("#lib-empty").classList.toggle("hidden", data.items.length > 0);
  $("#lib-total").textContent = data.items.length ? `${data.items.length} documents · ${fmtSize(data.total_size)}` : "";
  for (const d of data.items) {
    const el = document.createElement("div");
    el.className = "doc card";
    const parts = [];
    if (d.state === "parsing") parts.push("Reading the document…");
    else if (d.state === "failed") parts.push("Could not read: " + (d.error || "unknown error"));
    else {
      if (d.pages) parts.push(`${d.pages} pages`);
      parts.push(`${d.sections} section${d.sections === 1 ? "" : "s"}`);
      if (d.chunks_done) parts.push(fmtDur(d.seconds) + " of audio");
      if (d.chunks_total && d.chunks_done < d.chunks_total) parts.push(`generating ${d.chunks_done}/${d.chunks_total}`);
      if (d.voice_name) parts.push(d.voice_name);
    }
    parts.push(fmtSize(d.size));
    const pct = d.chunks_total ? Math.round((100 * d.chunks_done) / d.chunks_total) : 0;
    el.innerHTML = `<div class="body"><div class="t">${esc(d.title)}</div><div class="m">${esc(parts.join(" · "))}</div>
      ${d.chunks_total ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}</div>
      <div class="menu"><button class="icon-btn" aria-label="More">⋯</button></div>`;
    el.addEventListener("click", (e) => {
      if (e.target.closest(".menu")) return;
      if (d.state === "failed") return toast("This document could not be read. Delete it and try again.");
      location.hash = "#/read/" + d.id;
    });
    el.querySelector(".menu .icon-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      openDocMenu(el.querySelector(".menu"), d);
    });
    list.appendChild(el);
  }
  if (data.items.some((d) => d.state === "parsing" || (d.chunks_total && d.chunks_done < d.chunks_total))) {
    clearTimeout(loadLibrary.t);
    loadLibrary.t = setTimeout(() => { if (!$("#v-library").classList.contains("hidden")) loadLibrary(); }, 5000);
  }
}

function closePops() { $$(".pop").forEach((p) => p.remove()); }
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closePops(); });

function openDocMenu(host, d) {
  closePops();
  const pop = document.createElement("div");
  pop.className = "pop";
  pop.innerHTML = `<button data-a="open">Open</button><button data-a="rename">Rename</button>
    <button data-a="audio">Remove audio, keep text</button><button data-a="delete" class="danger">Delete from library</button>`;
  pop.addEventListener("click", async (e) => {
    const a = e.target.dataset.a;
    if (!a) return;
    closePops();
    try {
      if (a === "open") location.hash = "#/read/" + d.id;
      if (a === "rename") {
        const t = prompt("Title", d.title);
        if (t && t.trim()) { await api(`/api/docs/${d.id}/rename`, { json: { title: t } }); loadLibrary(); }
      }
      if (a === "audio" && confirm(`Remove the generated audio for “${d.title}”? The text stays, and you can generate again later.`)) {
        await api(`/api/docs/${d.id}/audio`, { method: "DELETE" }); loadLibrary(); toast("Audio removed");
      }
      if (a === "delete" && confirm(`Delete “${d.title}” and all of its audio? This can't be undone.`)) {
        await api(`/api/docs/${d.id}`, { method: "DELETE" }); loadLibrary(); toast("Deleted");
      }
    } catch (err) { toast(err.message); }
  });
  host.appendChild(pop);
}

/* ------------------------------------------------------------------ new document */

let newTab = "pdf";
function resetNew() {
  $("#new-status").textContent = "";
  $("#new-submit").disabled = false;
}
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  newTab = t.dataset.tab;
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".tabpane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== newTab));
}));
const drop = $("#drop");
$("#new-file").addEventListener("change", () => {
  const f = $("#new-file").files[0];
  $("#drop-label").textContent = f ? `${f.name} · ${fmtSize(f.size)}` : "Choose a PDF, or drop it here";
});
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) {
    $("#new-file").files = e.dataTransfer.files;
    $("#new-file").dispatchEvent(new Event("change"));
  }
});

$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  if (newTab === "pdf") {
    const f = $("#new-file").files[0];
    if (!f) return toast("Choose a file first");
    fd.append("file", f);
  } else {
    const t = $("#new-text").value;
    if (!t.trim()) return toast("Paste some text first");
    fd.append("text", t);
  }
  fd.append("title", $("#new-title").value);
  $("#new-submit").disabled = true;
  $("#new-status").textContent = "Uploading…";
  try {
    const { id } = await api("/api/docs", { method: "POST", body: fd });
    for (;;) {
      const m = await api(`/api/docs/${id}`);
      if (m.state === "ready") break;
      if (m.state === "failed") throw new Error(m.error || "Could not read this document");
      $("#new-status").textContent = m.note || "Reading the document…";
      await new Promise((r) => setTimeout(r, 1200));
    }
    $("#new-file").value = "";
    $("#new-text").value = "";
    $("#new-title").value = "";
    $("#drop-label").textContent = "Choose a PDF, or drop it here";
    location.hash = "#/read/" + id;
  } catch (err) {
    $("#new-status").textContent = err.message;
    $("#new-submit").disabled = false;
  }
});

/* ------------------------------------------------------------------ voices */

let VOICES = null;
async function loadVoices(refresh = false) {
  if (VOICES && !refresh) return VOICES;
  VOICES = await api("/api/voices" + (refresh ? "?refresh=1" : ""));
  return VOICES;
}
async function fillVoiceSelect(sel, current, refresh = false) {
  sel.innerHTML = `<option value="">Loading voices…</option>`;
  try {
    const vs = await loadVoices(refresh);
    sel.innerHTML = vs.length ? "" : `<option value="">No custom voices found</option>`;
    for (const v of vs) {
      const o = document.createElement("option");
      o.value = v.id;
      o.textContent = v.name + (v.language ? ` (${v.language})` : "");
      sel.appendChild(o);
    }
    const saved = current || localStorage.getItem("lector.voice");
    if (saved && vs.some((v) => v.id === saved)) sel.value = saved;
  } catch (e) {
    sel.innerHTML = `<option value="">Could not load voices</option>`;
    toast(e.message, 6000);
  }
}
const voiceName = (id) => (VOICES || []).find((v) => v.id === id)?.name || id;

/* ------------------------------------------------------------------ reader */

const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";

const R = {
  id: null, meta: null, doc: null, chunks: [], wordPara: null, skip: null,
  timings: new Map(), audio: new Audio(), cur: null, curEls: [], chunk: null, waitFor: null,
  view: "text", renderedSec: -1, page: 0, follow: true, userScrollAt: 0, pollT: null,
  rate: parseFloat(localStorage.getItem("lector.rate") || "1"), lastSave: 0, raf: 0, unlocked: false,
  pageWords: null,

  empty() { return !this.id; },

  close() {
    this.saveProgress(true);
    this.audio.pause();
    cancelAnimationFrame(this.raf);
    clearTimeout(this.pollT);
    this.id = null;
    this.doc = null;
  },

  async open(id) {
    if (this.id === id && this.doc) return;
    if (this.id) this.close();
    this.id = id;
    this.timings.clear();
    this.cur = null; this.chunk = null; this.waitFor = null; this.renderedSec = -1; this.curEls = [];
    $("#text-view").innerHTML = `<p class="not-ready">Opening…</p>`;
    $("#r-title").textContent = "";
    $("#r-sub").textContent = "";
    try {
      this.meta = await api(`/api/docs/${id}`);
      if (this.meta.state !== "ready") {
        $("#text-view").innerHTML = `<p class="not-ready">${esc(this.meta.state === "failed" ? this.meta.error : "Still reading the document…")}</p>`;
        if (this.meta.state === "parsing") this.pollT = setTimeout(() => { this.id = null; this.open(id); }, 2000);
        return;
      }
      this.doc = await api(`/api/docs/${id}/doc`);
    } catch (e) {
      $("#text-view").innerHTML = `<p class="not-ready">${esc(e.message)}</p>`;
      return;
    }
    this.index();
    $("#r-title").textContent = this.meta.title;
    document.title = this.meta.title + " · Lector";
    const isPdf = this.doc.source === "pdf";
    $("#view-toggle").classList.toggle("hidden", !isPdf);
    this.view = isPdf ? (localStorage.getItem("lector.view") || this.meta.view || "text") : "text";
    if (this.meta.rate) this.rate = this.meta.rate;
    this.setRate(this.rate, false);
    await this.refreshManifest();
    const local = parseInt(localStorage.getItem("lector.pos." + id) || "-1", 10);
    const start = Number.isInteger(this.meta.progress) ? this.meta.progress : (local >= 0 ? local : this.firstReadable());
    this.setView(this.view, false);
    this.setCursor(start, { scroll: "center" });
    this.updateStatus();
    if (!this.meta.voice) this.openSetup();
  },

  index() {
    const d = this.doc;
    this.wordPara = new Int32Array(d.words.length);
    d.paras.forEach((p, pi) => { for (let w = p.s; w < p.e; w++) this.wordPara[w] = pi; });
    this.skip = new Set(d.skipw);
    if (d.source === "pdf") {
      this.pageWords = Array.from({ length: d.pages }, () => []);
      d.words.forEach((w, i) => { if (w[1] >= 0) this.pageWords[w[1]].push(i); });
    }
  },

  firstReadable() {
    const p = this.doc.paras.find((p) => !SKIP_KINDS.has(p.k));
    return p ? p.s : 0;
  },
  secOf(w) { return this.doc.paras[this.wordPara[w]].sec; },

  /* ---- manifest & generation status */
  async refreshManifest() {
    if (!this.id) return;
    const id = this.id;
    let m;
    try { m = await api(`/api/docs/${id}/manifest`); } catch (_) { return; }
    if (id !== this.id) return;
    const prev = this.chunks;
    this.chunks = m.chunks;
    this.manifest = m;
    this.meta.voice = m.voice; this.meta.voice_name = m.voice_name; this.meta.style = m.style;
    // a chunk we were waiting for is ready
    if (this.waitFor && this.chunks[this.waitFor.chunk]?.status === "done") {
      const wf = this.waitFor;
      this.waitFor = null;
      this.playFromWord(wf.word, wf.autoplay);
    } else if (this.waitFor && this.chunks[this.waitFor.chunk]?.status === "error") {
      toast("That passage failed to generate: " + (this.chunks[this.waitFor.chunk].err || ""), 7000);
      this.waitFor = null;
      this.setPlayIcon();
    }
    if (prev.length && prev.some((c, i) => c.status !== this.chunks[i]?.status) && this.renderedSec >= 0) {
      this.renderNotReady();
    }
    this.renderToc();
    this.updateStatus();
    clearTimeout(this.pollT);
    const busy = this.chunks.some((c) => c.status === "pending" || c.status === "working");
    this.pollT = setTimeout(() => this.refreshManifest(), busy ? 2500 : 20000);
  },

  chunkForWord(w) {
    const cs = this.chunks;
    let lo = 0, hi = cs.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cs[mid].w0 <= w) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (best >= 0 && w <= cs[best].w1) return cs[best];
    return cs[best + 1] || null; // a skipped word: the next chunk after it
  },

  updateStatus() {
    if (!this.doc) return;
    const active = this.chunks.filter((c) => c.status !== "off");
    const done = active.filter((c) => c.status === "done").length;
    const errs = active.filter((c) => c.status === "error").length;
    const busy = active.some((c) => c.status === "pending" || c.status === "working");
    const pill = $("#gen-pill");
    if (busy && !this.manifest?.paused) {
      pill.textContent = `Generating audio · ${done} of ${active.length} passages ready`;
      pill.classList.remove("hidden");
    } else pill.classList.add("hidden");
    const secs = this.doc.sections.length;
    $("#r-sub").textContent = [this.meta.voice_name, `${secs} section${secs === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
    $("#s-gen").textContent = active.length
      ? `${done} of ${active.length} passages ready${errs ? ` · ${errs} failed` : ""}${this.manifest?.paused ? " · paused" : ""}`
      : "Nothing queued yet.";
    $("#s-pause").textContent = this.manifest?.paused ? "Resume" : "Pause";
    $("#s-retry").classList.toggle("hidden", !errs);
    const readySecs = new Set(this.chunks.filter((c) => c.status === "done").map((c) => c.sec));
    const queued = new Set(active.map((c) => c.sec));
    $("#dl-note").textContent = !readySecs.size ? "Downloads appear once audio has been generated."
      : readySecs.size < queued.size || busy ? `Includes the ${readySecs.size} section${readySecs.size === 1 ? "" : "s"} ready so far.` : "";
    $("#dl-full").disabled = $("#dl-zip").disabled = !readySecs.size;
  },

  /* ---- text view */
  renderSection(si) {
    const d = this.doc, sec = d.sections[si];
    const out = [];
    const firstP = d.paras.slice(sec.p0, sec.p1).find((p) => !SKIP_KINDS.has(p.k));
    const opensWithHeading = firstP && firstP.k === "heading";
    out.push(`${opensWithHeading ? "" : `<div class="sec-title">${esc(sec.title)}</div>`}<div id="sec-note"></div>`);
    for (let pi = sec.p0; pi < sec.p1; pi++) {
      const p = d.paras[pi];
      const skipP = SKIP_KINDS.has(p.k);
      const ws = [];
      for (let i = p.s; i < p.e; i++) {
        const mk = !skipP && this.skip.has(i);
        ws.push(`<span class="w${mk ? " mk" : ""}" data-i="${i}">${esc(d.words[i][0])}</span>`);
      }
      const cls = skipP ? `skip ${p.k}` : p.k;
      const tag = p.k === "heading" ? "h2" : "p";
      out.push(`<${tag} class="${cls}"${skipP ? ` data-label="${LABELS[p.k]}"` : ""}>${ws.join(" ")}</${tag}>`);
    }
    const prev = si > 0 ? d.sections[si - 1] : null, next = d.sections[si + 1];
    out.push(`<nav class="sec-nav">${prev ? `<button class="btn" data-sec="${si - 1}">← ${esc(prev.title)}</button>` : "<span></span>"}
      ${next ? `<button class="btn" data-sec="${si + 1}">${esc(next.title)} →</button>` : ""}</nav>`);
    $("#text-view").innerHTML = out.join("");
    this.renderedSec = si;
    this.curEls = [];
    this.renderNotReady();
  },

  renderNotReady() {
    const note = $("#sec-note");
    if (!note || this.renderedSec < 0) return;
    const cs = this.chunks.filter((c) => c.sec === this.renderedSec);
    let msg = "";
    if (cs.length && cs.every((c) => c.status === "off")) msg = "No audio for this section yet. Press play to generate it.";
    else if (cs.some((c) => c.status === "pending" || c.status === "working")) {
      const n = cs.filter((c) => c.status === "done").length;
      msg = `Generating this section · ${n} of ${cs.length} passages ready`;
    }
    note.innerHTML = msg ? `<p class="not-ready">${esc(msg)}</p>` : "";
  },

  /* ---- cursor & highlight */
  setCursor(w, opts = {}) {
    if (!this.doc || w == null || w < 0 || w >= this.doc.words.length) return;
    const same = this.cur === w;
    this.cur = w;
    const si = this.secOf(w);
    if (this.view === "text" && si !== this.renderedSec) this.renderSection(si);
    if (!same || opts.force) {
      this.curEls.forEach((el) => el.classList.remove("cur"));
      this.curEls = [];
      const ids = opts.group || [w];
      for (const i of ids) {
        const el = this.view === "text" ? $(`#text-view .w[data-i="${i}"]`) : null;
        if (el) { el.classList.add("cur"); this.curEls.push(el); }
      }
      if (this.view === "page") this.highlightOnPage(w, opts.scroll);
    }
    if (this.view === "text" && this.curEls[0]) this.autoScroll(this.curEls[0], opts.scroll);
    $("#p-section").textContent = this.doc.sections[si].title;
    $$(".toc-item").forEach((t) => t.classList.toggle("current", +t.dataset.sec === si));
    this.saveProgress();
  },

  autoScroll(el, mode) {
    const sc = $("#reader-scroll");
    if (!mode && Date.now() - this.userScrollAt < 5000) return;
    const r = el.getBoundingClientRect(), sr = sc.getBoundingClientRect();
    const band = sr.height;
    if (mode === "center" || r.top < sr.top + band * 0.18 || r.bottom > sr.top + band * 0.72) {
      const target = sc.scrollTop + (r.top - sr.top) - band * 0.38;
      sc.scrollTo({ top: Math.max(0, target), behavior: mode === "center" ? "auto" : "smooth" });
    }
  },

  saveProgress(force = false) {
    if (!this.id || this.cur == null) return;
    localStorage.setItem("lector.pos." + this.id, String(this.cur));
    const now = Date.now();
    if (!force && now - this.lastSave < 6000) return;
    this.lastSave = now;
    const body = JSON.stringify({ word: this.cur, view: this.view, rate: this.rate });
    if (force && navigator.sendBeacon) {
      navigator.sendBeacon(`/api/docs/${this.id}/progress`, new Blob([body], { type: "application/json" }));
    } else {
      fetch(`/api/docs/${this.id}/progress`, { method: "POST", headers: { "Content-Type": "application/json" }, body }).catch(() => {});
    }
  },

  /* ---- page view */
  setView(v, save = true) {
    if (v === "page" && this.doc?.source !== "pdf") v = "text";
    this.view = v;
    if (save) localStorage.setItem("lector.view", v);
    $$("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    $("#text-view").classList.toggle("hidden", v !== "text");
    $("#page-view").classList.toggle("hidden", v !== "page");
    this.renderedSec = -1;
    if (v === "text") $("#text-view").innerHTML = "";
    if (this.cur != null) this.setCursor(this.cur, { force: true, scroll: "center" });
  },

  showPage(n, then) {
    const d = this.doc;
    n = Math.max(0, Math.min(d.pages - 1, n));
    const img = $("#page-img");
    $("#pg-label").textContent = `Page ${n + 1} of ${d.pages}`;
    if (this.page === n && img.dataset.page === String(n) && img.complete) { then && then(); return; }
    this.page = n;
    $("#page-hl").classList.add("hidden");
    const scale = Math.min(3, Math.max(1, Math.ceil(((window.devicePixelRatio || 1) * Math.min(900, window.innerWidth)) / d.page_sizes[n][0])));
    img.onload = () => { then && then(); };
    img.dataset.page = String(n);
    img.src = `/api/docs/${this.id}/page/${n}.png?scale=${scale}`;
    // warm the next page
    if (n + 1 < d.pages) { const pre = new Image(); pre.src = `/api/docs/${this.id}/page/${n + 1}.png?scale=${scale}`; }
  },

  highlightOnPage(w, scrollMode) {
    const word = this.doc.words[w];
    if (word[1] < 0) return;
    const place = () => {
      const img = $("#page-img"), hl = $("#page-hl");
      const [pw] = this.doc.page_sizes[word[1]];
      const k = img.clientWidth / pw;
      if (!k) return;
      hl.style.left = (word[2] * k - 2) + "px";
      hl.style.top = (word[3] * k - 1) + "px";
      hl.style.width = ((word[4] - word[2]) * k + 4) + "px";
      hl.style.height = ((word[5] - word[3]) * k + 2) + "px";
      hl.classList.remove("hidden");
      this.autoScroll(hl, scrollMode);
    };
    if (word[1] !== this.page || $("#page-img").dataset.page !== String(word[1])) {
      if (this.follow || scrollMode) this.showPage(word[1], place);
      else $("#page-hl").classList.add("hidden");
    } else place();
  },

  wordAtPagePoint(x, y) {
    const list = this.pageWords?.[this.page] || [];
    let best = null, bestD = 1e9;
    for (const i of list) {
      const w = this.doc.words[i];
      const dx = x < w[2] ? w[2] - x : x > w[4] ? x - w[4] : 0;
      const dy = y < w[3] ? w[3] - y : y > w[5] ? y - w[5] : 0;
      const d = dx * dx + dy * dy * 4;
      if (d < bestD) { bestD = d; best = i; }
    }
    return bestD < 400 ? best : null;
  },

  /* ---- playback */
  unlock() {
    if (this.unlocked) return;
    this.unlocked = true;
    const a = this.audio;
    if (!a.src) { a.src = SILENT; a.play().then(() => a.pause()).catch(() => {}); }
  },

  async timingsFor(c) {
    if (this.timings.has(c.file)) return this.timings.get(c.file);
    const t = await api(`/api/docs/${this.id}/timing/${c.file}.json`);
    t.sort((a, b) => a[1] - b[1] || a[0] - b[0]);
    this.timings.set(c.file, t);
    return t;
  },

  timeForWord(t, w) {
    let best = null;
    for (const e of t) {
      if (e[0] === w) return e[1];
      if (e[0] > w && (best == null || e[1] < best)) best = e[1];
    }
    return best ?? 0;
  },

  async playFromWord(w, autoplay = true) {
    if (!this.doc) return;
    if (!this.meta.voice) return this.openSetup();
    const c = this.chunkForWord(w);
    if (!c) return toast("Nothing to read after this point.");
    this.setCursor(w);
    if (c.status !== "done") {
      this.waitFor = { chunk: c.i, word: w < c.w0 ? c.w0 : w, autoplay };
      this.audio.pause();
      this.setPlayIcon();
      try {
        await api(`/api/docs/${this.id}/focus`, { json: { chunk: c.i } });
        if (c.status === "off") toast("Generating this section…");
      } catch (e) { toast(e.message); }
      this.refreshManifest();
      return;
    }
    this.waitFor = null;
    const t = await this.timingsFor(c);
    await this.loadChunk(c, Math.max(0, this.timeForWord(t, w) - 0.05), autoplay);
  },

  async loadChunk(c, at, autoplay) {
    const a = this.audio;
    const url = `/api/docs/${this.id}/audio/${c.file}.mp3`;
    this.chunk = c.i;
    if (!a.src.endsWith(url)) {
      a.src = url;
      a.playbackRate = this.rate;
      await new Promise((res) => {
        const ok = () => { a.removeEventListener("loadedmetadata", ok); res(); };
        a.addEventListener("loadedmetadata", ok);
        a.load();
      });
    }
    a.currentTime = at;
    a.playbackRate = this.rate;
    if (autoplay) {
      try { await a.play(); } catch (e) { toast("Tap play to start"); }
    }
    this.setPlayIcon();
    this.mediaSession();
    this.prefetch(c.i + 1);
    this.tick();
  },

  prefetch(i) {
    const n = this.chunks[i];
    if (n?.status === "done") {
      fetch(`/api/docs/${this.id}/audio/${n.file}.mp3`).catch(() => {});
      this.timingsFor(n).catch(() => {});
    } else if (n && n.status !== "off") {
      api(`/api/docs/${this.id}/focus`, { json: { chunk: i } }).catch(() => {});
    }
  },

  tick() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      const c = this.chunks[this.chunk];
      if (c && this.timings.has(c.file)) {
        const t = this.timings.get(c.file);
        const now = this.audio.currentTime;
        let lo = 0, hi = t.length - 1, k = -1;
        while (lo <= hi) { const mid = (lo + hi) >> 1; if (t[mid][1] <= now + 0.03) { k = mid; lo = mid + 1; } else hi = mid - 1; }
        if (k >= 0) {
          const s = t[k][1];
          const group = [];
          for (let j = k; j >= 0 && t[j][1] === s; j--) group.push(t[j][0]);
          const w = Math.min(...group);
          if (w !== this.cur) this.setCursor(w, { group });
        }
      }
      if (!this.audio.paused) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },

  onEnded() {
    const next = this.chunks[this.chunk + 1];
    if (!next) { this.setPlayIcon(); toast("The end."); return; }
    if (next.status === "off") {
      this.setPlayIcon();
      this.setCursor(next.w0);
      toast("The next section hasn't been generated. Press play to generate it.", 5000);
      return;
    }
    this.playFromWord(next.w0, true);
  },

  togglePlay() {
    this.unlock();
    const a = this.audio;
    if (this.waitFor) { this.waitFor = null; this.setPlayIcon(); return; }
    if (!a.paused) { a.pause(); this.saveProgress(true); return; }
    const c = this.chunks[this.chunk];
    const curChunk = this.cur != null ? this.chunkForWord(this.cur) : null;
    if (c && c.status === "done" && a.src.includes(c.file) && curChunk && curChunk.i === c.i && a.currentTime > 0 && !a.ended) {
      a.play().catch(() => {});
      this.tick();
      this.setPlayIcon();
    } else {
      this.playFromWord(this.cur ?? this.firstReadable());
    }
  },

  async skip(sec) {
    const a = this.audio;
    const c = this.chunks[this.chunk];
    if (!c || !a.src) return;
    let t = a.currentTime + sec;
    if (t < 0) {
      const p = this.chunks[this.chunk - 1];
      if (p?.status === "done") { await this.timingsFor(p); return this.loadChunk(p, Math.max(0, p.dur + t), !a.paused); }
      t = 0;
    } else if (t > a.duration) {
      const n = this.chunks[this.chunk + 1];
      if (n?.status === "done") { await this.timingsFor(n); return this.loadChunk(n, Math.min(t - a.duration, n.dur), !a.paused); }
      t = Math.max(0, a.duration - 0.2);
    }
    a.currentTime = t;
    this.tick();
  },

  setRate(r, save = true) {
    this.rate = r;
    this.audio.playbackRate = r;
    $("#p-rate").textContent = (Number.isInteger(r) ? r : r.toFixed(2).replace(/0$/, "")) + "×";
    if (save) { localStorage.setItem("lector.rate", String(r)); this.lastSave = 0; this.saveProgress(); }
  },

  setPlayIcon() {
    const b = $("#p-play");
    const playing = !this.audio.paused && !this.audio.ended && this.audio.src !== SILENT;
    b.textContent = playing ? "❚❚" : "▶";
    b.classList.toggle("playing", playing);
    b.classList.toggle("waiting", !!this.waitFor);
    $("#p-status").textContent = this.waitFor ? "Waiting for audio…" : "";
  },

  mediaSession() {
    if (!("mediaSession" in navigator)) return;
    const si = this.cur != null ? this.secOf(this.cur) : 0;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: this.doc.sections[si]?.title || this.meta.title,
      artist: this.meta.voice_name || "Lector",
      album: this.meta.title,
    });
    const ms = navigator.mediaSession;
    ms.setActionHandler("play", () => this.togglePlay());
    ms.setActionHandler("pause", () => this.togglePlay());
    ms.setActionHandler("seekbackward", () => this.skip(-15));
    ms.setActionHandler("seekforward", () => this.skip(15));
    ms.setActionHandler("previoustrack", () => this.gotoSection(Math.max(0, si - 1), true));
    ms.setActionHandler("nexttrack", () => this.gotoSection(Math.min(this.doc.sections.length - 1, si + 1), true));
  },

  gotoSection(si, play = false) {
    const sec = this.doc.sections[si];
    let w = this.doc.paras[sec.p0].s;
    for (let pi = sec.p0; pi < sec.p1; pi++) if (!SKIP_KINDS.has(this.doc.paras[pi].k)) { w = this.doc.paras[pi].s; break; }
    if (play || !this.audio.paused) this.playFromWord(w, true);
    else { this.setCursor(w, { scroll: "center" }); $("#reader-scroll").scrollTop = 0; }
  },

  /* ---- contents */
  secState(si) {
    const cs = this.chunks.filter((c) => c.sec === si);
    if (!cs.length) return ["", "—"];
    const done = cs.filter((c) => c.status === "done").length;
    if (cs.some((c) => c.status === "error")) return ["bad", "failed"];
    if (done === cs.length) return ["ok", "ready"];
    if (cs.some((c) => c.status === "pending" || c.status === "working")) return ["go", `${done}/${cs.length}`];
    return ["", done ? `${done}/${cs.length}` : ""];
  },

  renderToc() {
    const box = $("#toc");
    if (!this.doc) return;
    const cur = this.cur != null ? this.secOf(this.cur) : -1;
    box.innerHTML = this.doc.sections.map((s, si) => {
      const [cls, label] = this.secState(si);
      const btn = cls === "ok" ? `<button class="mini" data-dl="${si}" title="Download this section">MP3</button>`
        : !cls && !label ? `<button class="mini" data-gen="${si}">Generate</button>` : "";
      return `<div class="toc-item${si === cur ? " current" : ""}" data-sec="${si}" data-level="${Math.min(3, s.level)}">
        <span class="tt">${esc(s.title)}</span><span class="st ${cls}">${esc(label)}</span>${btn}</div>`;
    }).join("");
  },

  /* ---- setup */
  async openSetup() {
    const d = this.doc;
    $("#setup-err").textContent = "";
    const box = $("#setup-secs");
    const lens = d.sections.map((s) => {
      let n = 0;
      for (let pi = s.p0; pi < s.p1; pi++) {
        const p = d.paras[pi];
        if (SKIP_KINDS.has(p.k)) continue;
        for (let i = p.s; i < p.e; i++) n += d.words[i][0].length + 1;
      }
      return n;
    });
    this.secLens = lens;
    box.innerHTML = d.sections.map((s, si) => `<label class="lvl${Math.min(3, s.level)}"><input type="checkbox" value="${si}" checked>
      <span>${esc(s.title)}</span><span class="len">${fmtDur((lens[si] / 900) * 60)}</span></label>`).join("");
    box.onchange = () => this.updateEstimate();
    const words = d.words.length;
    $("#setup-summary").textContent = `${d.source === "pdf" ? d.pages + " pages · " : ""}${words.toLocaleString()} words · ${d.sections.length} section${d.sections.length === 1 ? "" : "s"}${d.ocr_pages ? ` · ${d.ocr_pages} scanned page${d.ocr_pages === 1 ? "" : "s"} read with OCR` : ""}`;
    this.updateEstimate();
    $("#setup").classList.remove("hidden");
    $("#setup-style").value = this.meta.style || localStorage.getItem("lector.style") || "";
    await fillVoiceSelect($("#setup-voice"), this.meta.voice);
  },

  updateEstimate() {
    const sel = $$("#setup-secs input:checked").map((i) => +i.value);
    const chars = sel.reduce((a, si) => a + this.secLens[si], 0);
    $("#setup-est").textContent = sel.length ? `About ${fmtDur((chars / 900) * 60)} of audio for ${sel.length} section${sel.length === 1 ? "" : "s"}. Sections you leave out can be generated later from Contents.` : "Choose at least one section.";
  },
};

R.audio.preload = "auto";
R.audio.addEventListener("ended", () => R.onEnded());
R.audio.addEventListener("play", () => { R.setPlayIcon(); R.tick(); });
R.audio.addEventListener("pause", () => R.setPlayIcon());
R.audio.addEventListener("error", () => { if (R.audio.src && R.audio.src !== SILENT) { toast("Audio failed to load"); R.setPlayIcon(); } });

/* reader controls */
$("#p-play").addEventListener("click", () => R.togglePlay());
$("#p-back").addEventListener("click", () => R.skip(-15));
$("#p-fwd").addEventListener("click", () => R.skip(15));
$("#p-rate").addEventListener("click", () => {
  const i = RATES.indexOf(R.rate);
  R.setRate(RATES[(i + 1) % RATES.length]);
});
$$("#view-toggle button").forEach((b) => b.addEventListener("click", () => R.setView(b.dataset.view)));

$("#text-view").addEventListener("click", (e) => {
  const sn = e.target.closest("[data-sec]");
  if (sn) { R.gotoSection(+sn.dataset.sec); return; }
  const w = e.target.closest(".w");
  if (!w || (window.getSelection && String(window.getSelection()).length)) return;
  R.unlock();
  R.playFromWord(+w.dataset.i, true);
});
$("#page-frame").addEventListener("click", (e) => {
  const img = $("#page-img");
  const r = img.getBoundingClientRect();
  const k = img.clientWidth / R.doc.page_sizes[R.page][0];
  const w = R.wordAtPagePoint((e.clientX - r.left) / k, (e.clientY - r.top) / k);
  if (w != null) { R.unlock(); R.follow = true; R.playFromWord(w, true); }
});
$("#pg-prev").addEventListener("click", () => { R.follow = false; R.showPage(R.page - 1, () => R.cur != null && R.doc.words[R.cur][1] === R.page && R.highlightOnPage(R.cur)); });
$("#pg-next").addEventListener("click", () => { R.follow = false; R.showPage(R.page + 1, () => R.cur != null && R.doc.words[R.cur][1] === R.page && R.highlightOnPage(R.cur)); });

const sc = $("#reader-scroll");
["wheel", "touchmove"].forEach((ev) => sc.addEventListener(ev, () => (R.userScrollAt = Date.now()), { passive: true }));
window.addEventListener("resize", () => { if (R.view === "page" && R.cur != null) R.highlightOnPage(R.cur); });
document.addEventListener("visibilitychange", () => { if (document.hidden) R.saveProgress(true); });
window.addEventListener("pagehide", () => R.saveProgress(true));

/* keyboard */
document.addEventListener("keydown", (e) => {
  if ($("#v-reader").classList.contains("hidden") || e.target.matches("input, textarea, select")) return;
  if (e.code === "Space") { e.preventDefault(); R.togglePlay(); }
  if (e.code === "ArrowLeft") R.skip(-15);
  if (e.code === "ArrowRight") R.skip(15);
});

/* drawers */
function openDrawer(id) {
  $$(".drawer").forEach((d) => d.classList.add("hidden"));
  $("#" + id).classList.remove("hidden");
  $("#scrim").classList.remove("hidden");
}
function closeDrawers() {
  $$(".drawer").forEach((d) => d.classList.add("hidden"));
  $("#scrim").classList.add("hidden");
}
$("#scrim").addEventListener("click", closeDrawers);
$$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
$("#btn-contents").addEventListener("click", () => {
  R.renderToc();
  openDrawer("drawer-contents");
  $("#toc .current")?.scrollIntoView({ block: "center" });
});
$("#btn-settings").addEventListener("click", async () => {
  openDrawer("drawer-settings");
  R.updateStatus();
  $("#s-style").value = R.meta?.style || "";
  await fillVoiceSelect($("#s-voice"), R.meta?.voice);
});
$("#toc").addEventListener("click", async (e) => {
  const g = e.target.closest("[data-gen]"), dl = e.target.closest("[data-dl]");
  if (g) {
    e.stopPropagation();
    if (!R.meta.voice) { closeDrawers(); return R.openSetup(); }
    await api(`/api/docs/${R.id}/generate`, { json: { sections: [+g.dataset.gen] } });
    toast("Queued");
    return R.refreshManifest();
  }
  if (dl) { e.stopPropagation(); location.href = `/api/docs/${R.id}/export/section/${dl.dataset.dl}.mp3`; return; }
  const it = e.target.closest(".toc-item");
  if (it) { closeDrawers(); R.gotoSection(+it.dataset.sec); }
});

$("#s-voice-refresh").addEventListener("click", () => fillVoiceSelect($("#s-voice"), R.meta?.voice, true));
$("#setup-voice-refresh").addEventListener("click", () => fillVoiceSelect($("#setup-voice"), R.meta?.voice, true));
$("#s-apply").addEventListener("click", async () => {
  const v = $("#s-voice").value, st = $("#s-style").value.trim();
  if (!v) return toast("Choose a voice");
  if (v === R.meta.voice && st === (R.meta.style || "")) return toast("That voice is already in use");
  const hasAudio = R.chunks.some((c) => c.status === "done");
  if (hasAudio && !confirm("Regenerate the existing audio with this voice? The current audio will be replaced.")) return;
  R.audio.pause();
  const r = await api(`/api/docs/${R.id}/generate`, { json: { voice: v, voice_name: voiceName(v), style: st, sections: [], focus_chunk: R.cur != null ? R.chunkForWord(R.cur)?.i : 0 } });
  localStorage.setItem("lector.voice", v);
  localStorage.setItem("lector.style", st);
  R.timings.clear();
  R.audio.removeAttribute("src");
  R.chunk = null;
  toast(r.reset ? "Regenerating with the new voice…" : "Voice set");
  R.refreshManifest();
});
$("#s-gen-all").addEventListener("click", async () => {
  if (!R.meta.voice) { closeDrawers(); return R.openSetup(); }
  await api(`/api/docs/${R.id}/generate`, { json: {} });
  toast("Generating the whole document");
  R.refreshManifest();
});
$("#s-pause").addEventListener("click", async () => {
  await api(`/api/docs/${R.id}/pause`, { json: { paused: !R.manifest?.paused } });
  R.refreshManifest();
});
$("#s-retry").addEventListener("click", async () => {
  await api(`/api/docs/${R.id}/generate`, { json: { retry_errors: true, sections: [] } });
  R.refreshManifest();
});
function download(path, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Preparing…";
  location.href = path; // the server assembles the file, then the browser saves it
  setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 3000);
}
$("#dl-full").addEventListener("click", (e) => download(`/api/docs/${R.id}/export/full.mp3`, e.target));
$("#dl-zip").addEventListener("click", (e) => download(`/api/docs/${R.id}/export/sections.zip`, e.target));

/* setup modal */
$("#setup-all").addEventListener("click", () => { $$("#setup-secs input").forEach((i) => (i.checked = true)); R.updateEstimate(); });
$("#setup-none").addEventListener("click", () => { $$("#setup-secs input").forEach((i) => (i.checked = false)); R.updateEstimate(); });
$("#setup-later").addEventListener("click", () => $("#setup").classList.add("hidden"));
$("#setup-go").addEventListener("click", async () => {
  const v = $("#setup-voice").value;
  const secs = $$("#setup-secs input:checked").map((i) => +i.value);
  if (!v) return ($("#setup-err").textContent = "Choose a voice");
  if (!secs.length) return ($("#setup-err").textContent = "Choose at least one section");
  R.unlock();
  const st = $("#setup-style").value.trim();
  // start where the reader is if that section is selected, else at the first selected section
  let startWord = R.cur;
  if (startWord == null || !secs.includes(R.secOf(startWord))) {
    const s = R.doc.sections[secs[0]];
    startWord = R.doc.paras[s.p0].s;
  }
  const focus = R.chunkForWord(startWord)?.i ?? 0;
  try {
    await api(`/api/docs/${R.id}/generate`, { json: { voice: v, voice_name: voiceName(v), style: st, sections: secs, focus_chunk: focus } });
  } catch (e) { $("#setup-err").textContent = e.message; return; }
  localStorage.setItem("lector.voice", v);
  localStorage.setItem("lector.style", st);
  $("#setup").classList.add("hidden");
  R.meta.voice = v;
  R.meta.voice_name = voiceName(v);
  await R.refreshManifest();
  R.playFromWord(startWord, true);
});

/* boot */
if ("serviceWorker" in navigator) navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister()));
route();
