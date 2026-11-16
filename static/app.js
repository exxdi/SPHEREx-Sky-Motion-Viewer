/* SPHEREx Sky Motion Viewer — frontend logic.
 * Talks to the FastAPI backend (api.py). When the page is opened as a local file,
 * it calls the API at http://127.0.0.1:5050; when served by api.py, it uses the same origin.
 */
"use strict";

// Any unexpected script error is shown on the page instead of failing silently.
window.addEventListener("error", (e) => {
  const el = document.getElementById("status");
  if (el) { el.textContent = `Page error: ${e.message} (${(e.filename || "").split("/").pop()}:${e.lineno})`; el.classList.add("error"); }
});
window.addEventListener("unhandledrejection", (e) => {
  const el = document.getElementById("status");
  if (el) { el.textContent = `Page error: ${e.reason && e.reason.message ? e.reason.message : e.reason}`; el.classList.add("error"); }
});

const API = location.protocol === "file:" ? "http://127.0.0.1:5050" : "";
const PIX_DEG = 6.15 / 3600;      // must match api.py
const PM_YEARS = 10;              // Gaia arrow length = 10 years of motion
const STRETCH = 0.3;              // fixed server stretch; brightness/contrast are instant CSS filters
const EPOCH_COLORS = ["#5CC8E8", "#F5B342", "#F2867F", "#A98BFA", "#7BD88F"];
const DET_COLORS = { D1: "#A98BFA", D2: "#5CC8E8", D3: "#7BD88F", D4: "#F5B342", D5: "#F29A5C", D6: "#F2867F" };
const WISE_COLOR = "#F0A6F6";
const LAM_FULL = [0.75, 5.0];
const PNG_V = 2;                     // bump when the server renders images differently (browser cache)
// ---- date window (calendar): MJD, end exclusive
const dateToMjd = (iso) => Date.parse(`${iso}T00:00:00Z`) / 864e5 + 40587;
const mjdToDate = (mjd) => new Date((mjd - 40587) * 864e5).toISOString().slice(0, 10);
const inRange = (f) => (state.range[0] === null || f.mjd >= state.range[0]) && (state.range[1] === null || f.mjd < state.range[1]);
const rangeParams = () => ({ t0: state.range[0] ?? "", t1: state.range[1] ?? "" });
const DET_COLORS_K = { moving: "#7BD88F", fast: "#F2867F", brightening: "#F5B342", fading: "#F5B342" };
// Detection markers: colour = verdict (same as the legend), symbol in the tag = what kind of change.
const VERDICT_COL = { unmatched: "#7BD88F", known: "#9DB7FF", inconclusive: "#B8BED0" };
const KIND_GLYPH = { moving: "→", fast: "»", brightening: "▲", fading: "▼" };
const smoothParam = () => ($("smooth-toggle").checked ? { smooth: 1 } : {});
const epochMjd = (e) => { const fs = state.frames.filter((f) => f.epoch === e && inRange(f)); return fs.length ? fs.reduce((a, f) => a + f.mjd, 0) / fs.length : null; };
const activeEpochs = () => state.epochs.filter((e) => state.frames.some((f) => f.epoch === e && inRange(f)));
const DET_RANGE = { D1: [0.75, 1.1], D2: [1.1, 1.6], D3: [1.6, 2.4], D4: [2.4, 3.8], D5: [3.8, 4.4], D6: [4.4, 5.0] };
const RGB_BANDS = "blue 0.75–1.6 · green 1.6–3.2 · red 3.2–5.0 µm";
const lamIsFull = () => state.lam[0] <= LAM_FULL[0] + 1e-6 && state.lam[1] >= LAM_FULL[1] - 1e-6;
const inLam = (f) => lamIsFull() || (Number.isFinite(f.lam_um) && f.lam_um >= state.lam[0] && f.lam_um <= state.lam[1]);
const lamParams = () => (lamIsFull() ? {} : { lam_min: state.lam[0], lam_max: state.lam[1] });
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mjdToISO = (mjd) => new Date((mjd - 40587) * 86400000).toISOString();

const $ = (id) => document.getElementById(id);
const state = {
  ra: null, dec: null, fov: 0.1, det: "D2", view: "frames", name: "",
  frames: [], scale: {}, epochs: [], items: [], idx: 0,
  playing: false, timer: null, gaiaBase: null, click: null, asof: "", token: 0, loading: false,
  embed: false, chart: "spectrum", specPoints: null, bits: {}, job: null,
  wiseBase: null, marks: [], hover: null, lam: [0.75, 5.0], dets: [], detSel: null, detResult: null,
  range: [null, null], loadedRange: null, searchFrames: [],
};

const jyear = (mjd) => 2000 + (mjd - 51544.5) / 365.25;   // MJD -> decimal year
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
const POLL_MS = 1000;             // how often the page asks the server for newly ready frames

// One id per open page: the server cancels this page's old downloads when a new search starts,
// even if the page never received the old job id (request aborted half-way).
const CLIENT = Math.random().toString(36).slice(2, 10);

function cancelJob() {
  getJSON(`/api/frames/cancel?${qs({ job: state.job || "", client: CLIENT })}`, null).catch(() => {});
  state.job = null;
}

// Every request runs on a channel. A new search aborts every channel; a new request on the same
// channel (e.g. clicking another point for its spectrum) aborts only the previous one there.
const ctrls = {};
function channel(name) {
  if (ctrls[name]) ctrls[name].abort();
  ctrls[name] = new AbortController();
  return name;
}
function abortAll() {
  for (const k of Object.keys(ctrls)) { ctrls[k].abort(); delete ctrls[k]; }
}
const isAbort = (err) => Boolean(err && err.aborted);

// ---- settings (Settings card): sent with every request that depends on them
const covParam = () => ($("set-cov").value === "0.7" ? {} : { min_cov: $("set-cov").value });
const fastParam = () => ($("set-fast").value === "5" ? {} : { fast_min: $("set-fast").value });

// ------------------------------------------------------------------ helpers --
async function getJSON(path, chan = "search") {
  let r;
  const signal = chan ? (ctrls[chan] || (ctrls[chan] = new AbortController())).signal : undefined;
  try {
    r = await fetch(API + path, signal ? { signal } : undefined);
  } catch (err) {
    if (err && err.name === "AbortError") { const e = new Error("cancelled by a newer request"); e.aborted = true; throw e; }
    throw new Error("cannot reach the server. Is it running (python api.py or python flask_app.py)? Open the site via " +
      "http://127.0.0.1:5050/ (not by double-clicking index.html).");
  }
  let body = null;
  try { body = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) { const e = new Error((body && body.detail) || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return body;
}

function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function qs(obj) {
  return Object.entries(obj)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

function gridFov() {                       // real size of the backend grid
  const n = Math.max(8, Math.ceil(state.fov / PIX_DEG));
  return n * PIX_DEG;
}

// sky (deg) -> stage fraction (0..1, y from top). North up, East left.
function skyToFrac(ra, dec) {
  const f = gridFov();
  const dra = ((ra - state.ra + 540) % 360) - 180;
  return {
    x: 0.5 - (dra * Math.cos(state.dec * Math.PI / 180)) / f,
    y: 0.5 - (dec - state.dec) / f,
  };
}

function fracToSky(fx, fy) {
  const f = gridFov();
  const ra = state.ra + ((0.5 - fx) * f) / Math.cos(state.dec * Math.PI / 180);
  return { ra: (ra + 360) % 360, dec: state.dec + (0.5 - fy) * f };
}

// ---------------------------------------------------------------- URL state --
function readURL() {
  const p = new URLSearchParams(location.search);
  if (p.get("ra") && p.get("dec")) $("target").value = `${p.get("ra")} ${p.get("dec")}`;
  if (p.get("target")) $("target").value = p.get("target");
  if (p.get("fov")) $("fov").value = p.get("fov");
  if (p.get("det")) $("det").value = p.get("det");
  if (p.get("speed")) $("speed").value = p.get("speed");
  if (p.get("bright")) $("bright").value = p.get("bright");
  if (p.get("contrast")) $("contrast").value = p.get("contrast");
  applyLook();
  if (p.get("gaia") === "1") $("gaia-toggle").checked = true;
  if (p.get("flags") === "1") $("flags-toggle").checked = true;
  if (p.get("wise") === "1") $("wise-toggle").checked = true;
  if (p.get("epochs") === "1") $("epoch-toggle").checked = true;
  if (["0.5", "0.7", "0.9", "1"].includes(p.get("cov"))) $("set-cov").value = p.get("cov");
  if (["3", "4", "5"].includes(p.get("fastmin"))) $("set-fast").value = p.get("fastmin");
  applyEpochToggle(false);
  if (/^\d{4}-\d{2}-\d{2}$/.test(p.get("from") || "")) $("date-from").value = p.get("from");
  if (/^\d{4}-\d{2}-\d{2}$/.test(p.get("to") || "")) $("date-to").value = p.get("to");
  readRange();
  if (p.get("embed") === "1") { state.embed = true; document.body.classList.add("embed"); }
  if (["frames", "coadd", "diff", "rgb"].includes(p.get("view"))) state.view = p.get("view");
  const lam = (p.get("lam") || "").split(",").map(Number);
  if (lam.length === 2 && lam.every(Number.isFinite) && lam[0] < lam[1]) setLam(lam[0], lam[1], false);
  setViewButtons();
  return Boolean($("target").value.trim());
}

function writeURL() {
  const params = {
    target: $("target").value.trim(), fov: state.fov, det: state.det, view: state.view,
    speed: $("speed").value, bright: $("bright").value, contrast: $("contrast").value,
    gaia: $("gaia-toggle").checked ? 1 : "",
    flags: $("flags-toggle").checked ? 1 : "",
    wise: $("wise-toggle").checked ? 1 : "",
    epochs: $("epoch-toggle").checked ? 1 : "",
    cov: $("set-cov").value === "0.7" ? "" : $("set-cov").value,
    fastmin: $("set-fast").value === "5" ? "" : $("set-fast").value,
    embed: state.embed ? 1 : "",
    lam: lamIsFull() ? "" : `${state.lam[0]},${state.lam[1]}`,
    from: $("date-from").value, to: $("date-to").value,
  };
  history.replaceState(null, "", `${location.pathname}?${qs(params)}${location.hash}`);
  $("share-url").value = location.href;
  $("aladin-link").href = `https://aladin.cds.unistra.fr/AladinLite/?${qs({
    target: `${state.ra} ${state.dec}`, fov: state.fov * 2, survey: "CDS/P/allWISE/color",
  })}`;
}

// ------------------------------------------------------------------ loading --
function normalizeText(t) {          // curly quotes / unicode minus from copy-paste
  return t.replace(/[\u2018\u2019\u02BC]/g, "'").replace(/[\u2212\u2013\u2014]/g, "-")
    .replace(/\s+/g, " ").trim();
}

async function resolveTarget(text) {
  text = normalizeText(text);
  const m = text.match(/^\s*(-?\d+(?:\.\d+)?)[\s,]+([+-]?\d+(?:\.\d+)?)\s*$/);
  if (m) return { ra: parseFloat(m[1]), dec: parseFloat(m[2]) };
  return getJSON(`/api/resolve?${qs({ name: text })}`);   // names and 08h55m.. formats
}

// /api/search answers in ≤ 20 s: either the frame list or {pending:true} while IRSA is still working.
// We simply ask again — the server keeps ONE query to IRSA running, so there is no time limit and no error.
async function searchUntilDone(params, token, chan = "search") {
  for (;;) {
    const found = await getJSON(`/api/search?${qs(params)}`, chan === "search" ? undefined : channel(chan));
    if (!found || !found.pending) return found;
    if (token !== undefined && token !== state.token) return null;   // a newer search took over
    await sleep(300);
  }
}

async function load() {
  stop();
  abortAll();                             // drop every request of the previous search (search, frames, Gaia, …)
  cancelJob();                            // and stop the server downloading its frames
  dropPreloads();
  readRange();
  const token = ++state.token;            // a newer search cancels this one
  const text = $("target").value.trim();
  state.fov = parseFloat($("fov").value);
  state.det = $("det").value;
  state.click = null;
  state.gaiaBase = null;
  state.wiseBase = null;
  state.hover = null;
  state.dets = []; state.detSel = null; state.detResult = null;
  $("det-list").innerHTML = ""; $("det-status").textContent = ""; $("det-export").hidden = true;
  $("wise-status").textContent = "";
  state.frames = []; state.scale = {}; state.epochs = []; state.items = []; state.idx = 0;
  state.specPoints = null;
  renderTimeline(); show(0); drawChart();
  try {
    setStatus("Finding the target…");
    const pos = await resolveTarget(text);
    if (token !== state.token) return;
    state.ra = pos.ra; state.dec = pos.dec;
    state.targetPm = pos.pm_mas_yr && pos.pm_mas_yr.every(Number.isFinite)
      ? { pmra: pos.pm_mas_yr[0], pmdec: pos.pm_mas_yr[1], year: pos.epoch || 2026.0, name: state.name || text } : null;
    $("target-title").textContent = state.name || text;
    writeURL();

    const moved = pos.pm_mas_yr ? ` ${state.name || text} moves fast, so its position for ${pos.epoch} is used.` : "";
    setStatus(`Searching the SPHEREx archive (IRSA)…${moved}`);
    const started = Date.now();
    const tick = setInterval(() => {                 // IRSA can be slow: show that we are still waiting
      if (token !== state.token) { clearInterval(tick); return; }
      const sec = Math.round((Date.now() - started) / 1000);
      if (sec >= 4) {
        setStatus(`Searching the SPHEREx archive (IRSA)… ${sec} s` +
          (sec >= 12 ? " — IRSA is slow right now. We keep waiting for its answer (no time limit); " +
            "the result is saved, so next time this place opens instantly." : "") + moved);
      }
    }, 1000);
    let found;
    try {
      found = await searchUntilDone({ ra: state.ra, dec: state.dec }, token);
    } finally {
      clearInterval(tick);
    }
    if (!found || token !== state.token) return;
    if (!found.n_frames) {
      const d = found.irsa;
      setStatus(d && d.irsa_rows
        ? `IRSA answered with ${d.irsa_rows} rows, but none could be read as SPHEREx frames ` +
          `(bands: ${(d.bands_seen || []).join(", ") || "—"}). Please report this.`
        : "SPHEREx has no images of this point yet. New data arrives every week.", true);
      return;
    }
    state.searchFrames = Array.isArray(found.frames) ? found.frames : [];
    const saved = (found.retrieved_utc || "").slice(0, 16).replace("T", " ");
    state.staleNote = found.nearby
      ? ` IRSA is slow or down right now, so the frame list saved on ${saved} UTC for a point ` +
        `${found.nearby.sep_arcmin.toFixed(1)}′ away is used (same SPHEREx frames).`
      : found.stale ? ` IRSA is slow or down right now, so the frame list saved on ${saved} UTC is used (a fresh one loads in the background).` : "";
    renderEpochChips();
    loadOverlays();                               // catalogs need only the position: ask in parallel with frames
    const want = state.searchFrames.filter((f) => (state.det === "all" || f.det === state.det) && inRange(f));
    if (state.searchFrames.length && !want.length) {
      setStatus(`SPHEREx has no ${state.det === "all" ? "" : state.det + " "}images of this point between ` +
        `${$("date-from").value || "the start"} and ${$("date-to").value || "now"}. Pick one of the observation dates below.`, true);
      return;
    }

    // Frames stream in: the server downloads in the background (epochs interleaved),
    // the page picks up whatever is ready every second and shows it right away.
    state.loading = true;
    state.loadedRange = [...state.range];
    const job = await getJSON(`/api/frames/start?${qs({
      ra: state.ra, dec: state.dec, fov: state.fov, det: state.det, client: CLIENT, ...rangeParams(),
      min_coverage: $("set-cov").value,
    })}`);
    if (token !== state.token) { getJSON(`/api/frames/cancel?${qs({ job: job.job })}`, null).catch(() => {}); return; }
    state.job = job.job;
    let since = 0, pending = [], res = null;
    for (;;) {
      res = await getJSON(`/api/frames/status?${qs({ job: job.job, since })}`);
      if (token !== state.token) return;
      since = res.next;
      state.asof = res.data_as_of;
      pending = pending.concat(res.frames);
      for (const [d, sc] of Object.entries(res.scale)) if (!state.scale[d]) state.scale[d] = sc;
      const ready = pending.filter((f) => state.scale[f.det]);   // show once its brightness scale is fixed
      if (ready.length) {
        pending = pending.filter((f) => !state.scale[f.det]);
        const cur = state.items[state.idx];
        state.frames = state.frames.concat(ready).sort((a, b) => a.mjd - b.mjd);
        state.epochs = [...new Set(state.frames.map((f) => f.epoch))].sort((a, b) => a - b);
        const keep = state.view === "frames" && cur && cur.id
          ? Math.max(0, state.frames.findIndex((f) => f.id === cur.id)) : state.idx;
        const wasPlaying = state.playing;
        buildSequence(keep);                             // the frame on screen stays on screen
        if (wasPlaying) play();
        if (state.specPoints) drawChart();
      }
      if (res.finished) break;
      setStatus(`Loading from IRSA: ${res.done} of ${res.total} frames processed, ` +
        `${state.frames.length} on screen. You can press Play already.${state.staleNote || ""}`);
      await sleep(POLL_MS);
    }
    state.job = null;
    if (state.click) loadSpectrum(state.click);           // measured while loading -> now with every frame
    const requested = res.total, lowCov = res.n_low_coverage || 0;
    const lastErr = res.errors && res.errors.length ? res.errors[0] : "";

    state.loading = false;
    if (state.view === "diff" && activeEpochs().length >= 2) buildSequence(state.idx);
    if (!state.frames.length) {
      const why = lastErr ? ` Server error: ${lastErr}`
        : lowCov ? ` ${lowCov} frame(s) only partly cover this field (target near a detector edge) — try a smaller field ` +
          "or lower the coverage limit in Settings."
        : "";
      setStatus(`No usable frames for detector ${state.det}.${why}`, true);
      return;
    }
    $("m-asof").textContent =
      `IRSA data as of ${state.asof} · ${state.frames.length} of ${requested} frames usable`;
    const shown = state.frames.filter(inRange);
    const allMjd = (shown.length ? shown : state.frames).map((f) => f.mjd);
    setStatus(`${allMjd.length} frames ready, observed ${fmtSpan(Math.min(...allMjd), Math.max(...allMjd))}` +
      `${showEpochs() ? ` in ${activeEpochs().length || state.epochs.length} epoch(s)` : ""}. Press Play.` +
      (lowCov ? ` (${lowCov} frame(s) taken at the detector edge, not covering the target, are hidden.)` : ""));
    loadOverlays();                               // no-op if they already arrived during loading
  } catch (err) {
    if (isAbort(err) || token !== state.token) return;           // a newer search took over
    state.loading = false;
    setStatus(`Something went wrong: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------- sequences --
function framePng(f) {
  const [vmin, vmax] = state.scale[f.det] || [null, null];
  return `${API}${f.png}&${qs({ vmin, vmax, stretch: STRETCH, ...smoothParam(), pv: PNG_V })}`;
}

// SPHEREx observes a point in a burst of a few days about every six months. The server groups those
// frames (field "epoch"); the interface never shows the group itself, only its dates.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtSpan(lo, hi) {
  const a = new Date((lo - 40587) * 864e5), b = new Date((hi - 40587) * 864e5);
  const d = (x) => x.getUTCDate(), m = (x) => MONTHS[x.getUTCMonth()], y = (x) => x.getUTCFullYear();
  if (y(a) === y(b) && m(a) === m(b)) return d(a) === d(b) ? `${d(a)} ${m(a)} ${y(a)}` : `${d(a)}–${d(b)} ${m(a)} ${y(a)}`;
  if (y(a) === y(b)) return `${d(a)} ${m(a)} – ${d(b)} ${m(b)} ${y(a)}`;
  return `${d(a)} ${m(a)} ${y(a)} – ${d(b)} ${m(b)} ${y(b)}`;
}
function periodFrames(e) {                    // the period's frames inside the calendar range
  const fs = state.frames.filter((f) => f.epoch === e && inRange(f));
  return fs.length ? fs : state.searchFrames.filter((f) => f.epoch === e && inRange(f));
}
function periodSpan(e) {
  const ms = periodFrames(e).map((f) => f.mjd);
  return ms.length ? [Math.min(...ms), Math.max(...ms)] : null;
}
function epochDate(e) {                       // e.g. "13–15 Aug 2025"
  const sp = periodSpan(e);
  return sp ? fmtSpan(sp[0], sp[1]) : "";
}
function shortDate(e) {                       // e.g. "Aug 2025" (ribbon buttons)
  const sp = periodSpan(e);
  if (!sp) return "";
  const a = new Date((sp[0] - 40587) * 864e5);
  return `${MONTHS[a.getUTCMonth()]} ${a.getUTCFullYear()}`;
}
// Epochs are hidden by default; the "Show epochs" checkbox brings their numbers back next to the dates.
const showEpochs = () => $("epoch-toggle").checked;
const epochTag = (e) => (showEpochs() ? `Epoch ${e + 1} · ` : "");

function periodParams(e) {                    // the period's dates ∩ the calendar range, as an API date window
  const all = (state.searchFrames.length ? state.searchFrames : state.frames).filter((f) => f.epoch === e).map((f) => f.mjd);
  let t0 = all.length ? Math.min(...all) : null, t1 = all.length ? Math.max(...all) + 1 / 1440 : null;
  if (state.range[0] !== null) t0 = t0 === null ? state.range[0] : Math.max(t0, state.range[0]);
  if (state.range[1] !== null) t1 = t1 === null ? state.range[1] : Math.min(t1, state.range[1]);
  return { t0: t0 ?? "", t1: t1 ?? "" };
}

function epochYear(e) {
  const f = state.frames.find((x) => x.epoch === e && inRange(x)) || state.frames.find((x) => x.epoch === e);
  return f ? jyear(f.mjd) : null;
}

const preloaded = new Set();
let preloadImgs = [];
function dropPreloads() {              // stop the browser downloading images of the previous search
  for (const im of preloadImgs) im.src = "";
  preloadImgs = [];
}
const countIn = (e) => state.frames.filter((f) => f.epoch === e && inRange(f)).length;   // cache-buster

function buildSequence(startIdx = 0) {
  const base = { ra: state.ra, dec: state.dec, fov: state.fov, det: state.det };
  const stretch = STRETCH;
  if (state.view === "rgb") {
    state.items = state.det !== "all" ? [] : activeEpochs().map((e) => ({
      src: `${API}/api/rgb.png?${qs({ ra: state.ra, dec: state.dec, fov: state.fov, stretch, n: countIn(e), ...periodParams(e), ...covParam(), pv: PNG_V })}`,
      label: showEpochs() ? `E${e + 1}` : shortDate(e), epoch: e, date: epochDate(e), det: "D1–D6", kind: "rgb", t: epochYear(e), mjd: epochMjd(e),
    }));
    if (state.det !== "all") {
      setStatus("Color mixes three infrared ranges, so it needs every detector: set Detector to " +
        "All (D1–D6) and press Show the sky.", true);
    }
  } else if (state.view === "coadd") {
    state.items = activeEpochs().map((e) => ({
      src: `${API}/api/coadd.png?${qs({ ...base, stretch, n: countIn(e), ...lamParams(), ...smoothParam(), ...periodParams(e), ...covParam(), pv: PNG_V })}`,
      label: showEpochs() ? `E${e + 1}` : shortDate(e), epoch: e, date: epochDate(e), det: state.det, kind: "coadd",
      t: epochYear(e), mjd: epochMjd(e),
    }));
  } else if (state.view === "diff") {
    state.items = [];
    const eps = activeEpochs();
    for (let i = 1; i < eps.length; i++) {
      const a = eps[i - 1], b = eps[i];
      const pa = periodParams(a), pb = periodParams(b);
      const win = { a_t0: pa.t0, a_t1: pa.t1, b_t0: pb.t0, b_t1: pb.t1 };
      state.items.push({
        // clean difference: only frames at the same wavelength, median of all pairs, star-sized filter
        src: `${API}/api/detect/map.png?${qs({ ...base, ...win, n: countIn(a) + countIn(b), ...lamParams(), ...covParam(), pv: PNG_V })}`,
        fallback: `${API}/api/diff.png?${qs({ ...base, ...win, n: countIn(a) + countIn(b), ...lamParams(), ...covParam(), pv: PNG_V })}`,
        label: showEpochs() ? `E${b + 1}−E${a + 1}` : `${shortDate(b)} vs ${shortDate(a)}`, epoch: b,
        date: `${epochDate(a)} → ${epochDate(b)}`,
        det: state.det, kind: "diff", t: epochYear(b), mjd: epochMjd(b),
      });
    }
    if (!state.items.length) {
      if (state.loading) {
        setStatus("Clean difference compares images taken on different dates — the later dates are still loading " +
          "and will appear here automatically. Meanwhile you can watch Frames.");
      } else {
        setStatus(state.range[0] !== null || state.range[1] !== null
          ? "Clean difference needs images from at least two separate observation dates inside the chosen range — widen the range."
          : "Clean difference needs images from two separate observation dates. SPHEREx has observed this point only once so far.", true);
      }
    }
  } else {
    state.items = state.frames.filter(inLam).filter(inRange).map((f, i) => ({
      src: framePng(f), label: String(i + 1), epoch: f.epoch, date: f.date, id: f.id,
      t: jyear(f.mjd), mjd: f.mjd,
      det: f.det, lam: f.lam_um, ver: f.version, flagged: f.flagged_px, kind: "frame", coverage: f.coverage,
    }));
  }
  if (!state.items.length && state.frames.length && state.view === "frames" && !lamIsFull()) {
    const dr = DET_RANGE[state.det];
    setStatus(`No frames between ${state.lam[0]} and ${state.lam[1]} µm` +
      (dr ? ` — detector ${state.det} only covers ${dr[0]}–${dr[1]} µm. Choose another window or Detector All.` : " yet."), true);
  }
  state.items.forEach((it) => {                   // preload, once per image
    if (!preloaded.has(it.src)) {
      preloaded.add(it.src);
      const im = new Image();
      im.onload = im.onerror = () => { preloadImgs = preloadImgs.filter((x) => x !== im); };
      im.src = it.src;
      preloadImgs.push(im);
    }
  });
  renderTimeline();
  show(startIdx);
}

function renderTimeline() {
  const tl = $("timeline");
  tl.innerHTML = "";
  let lastEpoch = null;
  state.items.forEach((it, i) => {
    if (it.kind === "frame" && it.epoch !== lastEpoch) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = epochTag(it.epoch) + epochDate(it.epoch);
      tl.appendChild(sep);
      lastEpoch = it.epoch;
    }
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = it.label;
    b.setAttribute("aria-label", `Show ${it.kind} ${it.label}`);
    b.addEventListener("click", () => { stop(); show(i); });
    tl.appendChild(b);
  });
}

function show(i) {
  if (!state.items.length) {
    $("frame-img").hidden = true; $("stage-empty").hidden = false; $("frame-label").hidden = true;
    $("stage-empty").textContent = state.loading ? "Loading frames…" : "No frames loaded";
    $("flags-img").hidden = true; $("fits-link").hidden = true; $("m-bits").textContent = "";
    ["m-date", "m-epoch", "m-det", "m-lam", "m-ver", "m-flag"].forEach((id) => ($(id).textContent = "—"));
    drawOverlay();
    return;
  }
  state.idx = (i + state.items.length) % state.items.length;
  const it = state.items[state.idx];
  const img = $("frame-img");
  img.onerror = it.fallback ? () => { img.onerror = null; img.src = it.fallback; } : null;   // too few λ matches
  img.src = it.src;
  img.hidden = false;
  $("stage-empty").hidden = true;

  const lam = Number.isFinite(it.lam) ? ` · λ ${it.lam.toFixed(3)} µm` : "";
  const what = it.kind === "frame" ? `Frame ${state.idx + 1} of ${state.items.length}`
    : it.kind === "coadd" ? `${showEpochs() ? `Co-add, epoch ${it.epoch + 1}` : "Stacked frames"}${lamIsFull() ? "" : ` · ${state.lam[0]}–${state.lam[1]} µm`}`
    : it.kind === "rgb" ? `Color (${RGB_BANDS})${showEpochs() ? `, epoch ${it.epoch + 1}` : ""}`
    : `Clean difference${showEpochs() ? ` ${it.label}` : ", later minus earlier"} (same wavelengths; white = appeared, black = gone)`;
  const when = it.kind === "frame" ? it.date.slice(0, 19) : it.date;   // diff: "A → B"
  const gap = it.kind === "frame" && Number.isFinite(it.coverage) && it.coverage < 0.97
    ? ` · striped = not covered by this frame (${Math.round(100 * (1 - it.coverage))}%)` : "";
  $("frame-label").textContent = `${what} · ${when}${lam}${gap}`;
  $("frame-label").hidden = false;

  $("m-date").textContent = when || "—";
  $("m-epoch").textContent = it.epoch !== undefined ? it.epoch + 1 : "—";
  $("m-det").textContent = it.det;
  $("m-lam").textContent = Number.isFinite(it.lam) ? `${it.lam.toFixed(3)} µm` : "—";
  $("m-ver").textContent = it.ver || "—";
  $("m-flag").textContent = it.flagged !== undefined ? it.flagged : "—";

  [...$("timeline").querySelectorAll("button")].forEach((b, k) =>
    b.classList.toggle("on", k === state.idx));
  drawOverlay();
  updateFrameExtras(it);
}

// FITS download link, flags layer and flag-bit summary for the current frame.
function updateFrameExtras(it) {
  const base = { ra: state.ra, dec: state.dec, fov: state.fov };
  const isFrame = it && it.kind === "frame" && it.id;
  $("fits-link").hidden = !isFrame;
  if (isFrame) $("fits-link").href = `${API}/api/fits/${it.id}?${qs(base)}`;

  const flagsImg = $("flags-img");
  if (isFrame && $("flags-toggle").checked) {
    flagsImg.src = `${API}/api/flags/${it.id}.png?${qs(base)}`;
    flagsImg.hidden = false;
  } else {
    flagsImg.hidden = true;
  }

  $("m-bits").textContent = "";
  if (!isFrame || state.playing) return;           // no extra requests while playing
  const show = (info) => {
    if (!info.bits || !info.bits.length) return;
    const top = [...info.bits].sort((a, b) => b.pixels - a.pixels).slice(0, 4)
      .map((b) => `bit ${b.bit}: ${b.pixels}`).join(" · ");
    $("m-bits").textContent = `Most common flags — ${top}`;
  };
  if (state.bits[it.id]) { show(state.bits[it.id]); return; }
  getJSON(`/api/flags/${it.id}.json?${qs(base)}`, channel("flags"))
    .then((info) => { state.bits[it.id] = info; if (state.items[state.idx] === it) show(info); })
    .catch(() => {});
}

// ------------------------------------------------------------------ playback --
function play() {
  if (state.items.length < 2) return;
  state.playing = true;
  $("play").textContent = "Pause";
  clearInterval(state.timer);
  state.timer = setInterval(() => show(state.idx + 1), 1000 / parseFloat($("speed").value));
}

function stop() {
  const was = state.playing;
  state.playing = false;
  $("play").textContent = "Play";
  clearInterval(state.timer);
  if (was && state.items[state.idx]) updateFrameExtras(state.items[state.idx]);
}

// -------------------------------------------------------------------- Gaia --
// Catalog overlays. The server waits ~25 s for a catalog, then answers 504 while the query keeps
// running in the background and fills its cache — so on 504 we simply ask again (up to ~3 min).
async function fetchCatalog(path, chan, statusEl, label, token, stillWanted) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await getJSON(path, channel(chan));
    } catch (err) {
      if (isAbort(err) || token !== state.token || !stillWanted()) return null;
      if (err.status === 504 && attempt < 8) {
        $(statusEl).textContent = `${label} is answering slowly — still loading in the background, retrying…`;
        await sleep(20000);
        if (token !== state.token || !stillWanted()) return null;
        continue;
      }
      $(statusEl).textContent = `${label} unavailable: ${err.message}`;
      return null;
    }
  }
}

const inflight = {};          // one request per catalog at a time (a second call joins the first)
function once(key, fn) {
  if (!inflight[key]) inflight[key] = fn().finally(() => { delete inflight[key]; });
  return inflight[key];
}

function loadGaia() {
  return once(`gaia:${state.token}:${$("gaia-depth").value}`, loadGaiaNow);
}

function loadWise() {
  return once(`wise:${state.token}`, loadWiseNow);
}

async function loadGaiaNow() {
  if (state.ra === null) return;
  if (!state.gaiaBase) {
    const token = state.token, depth = $("gaia-depth").value;
    $("gaia-status").textContent = "Loading stars from Gaia DR3 (ESA archive or the VizieR mirror)…";
    const res = await fetchCatalog(`/api/gaia?${qs({ ra: state.ra, dec: state.dec, fov: state.fov, gmax: depth })}`,
      "gaia", "gaia-status", "Gaia", token, () => $("gaia-toggle").checked && depth === $("gaia-depth").value);
    if (!res || token !== state.token || depth !== $("gaia-depth").value) return;  // target / depth changed meanwhile
    if (!Number.isFinite(res.epoch_jyear)) res.epoch_jyear = 2016.0;
    state.gaiaBase = res;
  }
  if (!$("gaia-toggle").checked) return;
  const fast = state.gaiaBase.stars.filter((x) => Math.hypot(x.pmra, x.pmdec) > 500).length;
  $("gaia-status").textContent = `${state.gaiaBase.n} Gaia stars in the field` +
    (fast ? ` · ${fast} fast mover(s) (> 0.5″/yr)` : "") +
    (state.gaiaBase.source ? ` · via ${state.gaiaBase.source}` : "");
  drawOverlay();
}

async function loadWiseNow() {
  if (state.ra === null) return;
  if (!state.wiseBase) {
    const token = state.token;
    $("wise-status").textContent = "Loading AllWISE sources…";
    const res = await fetchCatalog(`/api/wise?${qs({ ra: state.ra, dec: state.dec, fov: state.fov })}`,
      "wise", "wise-status", "AllWISE", token, () => $("wise-toggle").checked);
    if (!res || token !== state.token) return;
    if (!Number.isFinite(res.epoch_jyear)) res.epoch_jyear = 2010.5;
    state.wiseBase = res;
  }
  if (!$("wise-toggle").checked) return;
  $("wise-status").textContent = `${state.wiseBase.n} AllWISE sources (W1 < 15) · via ${state.wiseBase.source}`;
  drawOverlay();
}

// Both overlays at once, without holding anything else up.
function loadOverlays() {
  if ($("gaia-toggle").checked) loadGaia();
  if ($("wise-toggle").checked) loadWise();
}

// Catalog positions -> position on the date of the frame (linear proper motion).
function catAt(base, year) {
  if (!base || year === null || year === undefined) return [];
  const dt = year - base.epoch_jyear;
  return base.stars.map((s) => ({
    ...s,
    ra: s.ra + (s.pmra * dt / 3.6e6) / Math.cos(s.dec * Math.PI / 180),
    dec: s.dec + s.pmdec * dt / 3.6e6,
  }));
}

const gaiaAt = (year) => catAt(state.gaiaBase, year);

function drawOverlay() {
  const svg = $("overlay");
  svg.setAttribute("viewBox", "0 0 1000 1000");
  svg.setAttribute("preserveAspectRatio", "none");   // overlay always matches the image box exactly
  let html = "";
  const it = state.items[state.idx];
  state.marks = [];
  if (!it) { svg.innerHTML = ""; hideTip(); return; }      // no image -> no circles or markers
  const stars = it && $("gaia-toggle").checked ? gaiaAt(it.t) : null;
  if (stars && stars.length) {
    const f = gridFov();
    for (const s of stars) {
      const p = skyToFrac(s.ra, s.dec);
      if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) continue;
      const x = p.x * 1000, y = p.y * 1000;
      const dx = -(s.pmra * PM_YEARS / 3.6e6) / f * 1000;   // East is left
      const dy = -(s.pmdec * PM_YEARS / 3.6e6) / f * 1000;  // North is up
      const r = s.g === null ? 6 : Math.max(4, 14 - s.g / 2);
      const fast = Math.hypot(s.pmra, s.pmdec) > 500;          // > 0.5"/yr: highlight
      const col = fast ? "#F5B342" : "#5CC8E8";
      html += `<circle cx="${x}" cy="${y}" r="${fast ? r + 8 : r}" fill="none" stroke="${col}" stroke-width="${fast ? 3 : 2}" opacity="0.85"/>`;
      state.marks.push({ x, y, kind: "gaia", s });
      if (Math.hypot(dx, dy) > 4) {
        html += `<line x1="${x}" y1="${y}" x2="${x + dx}" y2="${y + dy}" stroke="${col}" stroke-width="${fast ? 4 : 2}"/>` +
          `<circle cx="${x + dx}" cy="${y + dy}" r="${fast ? 5 : 3}" fill="${col}"/>`;
      }
    }
  }
  const wise = it && $("wise-toggle").checked ? catAt(state.wiseBase, it.t) : null;
  if (wise && wise.length) {
    const f = gridFov();
    for (const s of wise) {
      const p = skyToFrac(s.ra, s.dec);
      if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) continue;
      const x = p.x * 1000, y = p.y * 1000;
      html += `<rect x="${x - 11}" y="${y - 11}" width="22" height="22" fill="none" stroke="${WISE_COLOR}" stroke-width="2" opacity="0.9"/>`;
      const dx = -(s.pmra * PM_YEARS / 3.6e6) / f * 1000, dy = -(s.pmdec * PM_YEARS / 3.6e6) / f * 1000;
      if (Math.hypot(dx, dy) > 4) {
        html += `<line x1="${x}" y1="${y}" x2="${x + dx}" y2="${y + dy}" stroke="${WISE_COLOR}" stroke-width="2"/>`;
      }
      state.marks.push({ x, y, kind: "wise", s });
    }
  }
  if ($("det-show").checked && state.dets.length) html += drawDetections(it);
  if (state.hover && state.hover.kind !== "det") {    // keep the hovered catalog source highlighted
    const h = state.marks.find((m) => markKey(m) === markKey(state.hover));
    if (h) html += `<circle cx="${h.x}" cy="${h.y}" r="26" fill="none" stroke="#FFFFFF" stroke-width="2" stroke-dasharray="6 5"/>`;
  }
  if (state.click) {
    const c = clickAt(state.click, it.t);
    const p = skyToFrac(c.ra, c.dec);
    const x = p.x * 1000, y = p.y * 1000;
    html += `<circle cx="${x}" cy="${y}" r="18" fill="none" stroke="#F5B342" stroke-width="3"/>` +
      `<line x1="${x - 30}" y1="${y}" x2="${x - 20}" y2="${y}" stroke="#F5B342" stroke-width="3"/>` +
      `<line x1="${x + 20}" y1="${y}" x2="${x + 30}" y2="${y}" stroke="#F5B342" stroke-width="3"/>`;
  }
  svg.innerHTML = html;
}

// ------------------------------------------------------- automatic detection --
function drawDetections(it) {
  let under = "", over = "";
  const placed = [];                               // tag boxes already used (no overlaps)
  const focus = state.hover && state.hover.kind === "det" ? state.hover.s.id : state.detSel;
  const visible = [];
  for (const d of state.dets) {
    const p = detPosAt(d, it);
    if (!p) continue;
    const fr = skyToFrac(p.ra, p.dec);
    if (fr.x < -0.05 || fr.x > 1.05 || fr.y < -0.05 || fr.y > 1.05) continue;
    visible.push({ d, x: fr.x * 1000, y: fr.y * 1000 });
  }
  const halo = 'fill="none" stroke="#05070f" stroke-opacity="0.7" stroke-linecap="round" stroke-linejoin="round"';
  for (const { d, x, y } of visible) {
    const col = VERDICT_COL[d.verdict_class] || VERDICT_COL.inconclusive;
    const on = focus === d.id;                                   // hovered or selected
    const dim = focus !== null && focus !== undefined && !on ? 0.55 : 1;
    const op = (d.verdict_class === "inconclusive" ? 0.8 : 1) * dim;
    const r = on ? 22 : 18, w = on ? 3.5 : 2.5;
    const pts = d.points.map((q) => { const f2 = skyToFrac(q.ra, q.dec); return [f2.x * 1000, f2.y * 1000]; });
    // its path: only for the hovered / selected one, so the picture stays clean
    if (on && pts.length > 1) {
      under += `<polyline points="${pts.map((q) => q.join(",")).join(" ")}" fill="none" stroke="${col}" stroke-width="2" ` +
        `stroke-opacity="0.8" stroke-dasharray="2 7" stroke-linecap="round"/>`;
      for (const [px, py] of pts) under += `<circle cx="${px}" cy="${py}" r="4" fill="${col}" fill-opacity="0.85"/>`;
    }
    // marker: ring (slow mover / brightness change) or diamond (fast mover), dark outline for contrast
    const shape = d.kind === "fast"
      ? `<rect x="${x - r * 0.75}" y="${y - r * 0.75}" width="${r * 1.5}" height="${r * 1.5}" rx="3" transform="rotate(45 ${x} ${y})"`
      : `<circle cx="${x}" cy="${y}" r="${r}"`;
    over += `${shape} ${halo} stroke-width="${w + 3}" opacity="${op}"/>` +
      `${shape} fill="none" stroke="${col}" stroke-width="${w}" opacity="${op}"/>`;
    // short arrow: which way it moves
    if (pts.length > 1) {
      const [ax, ay] = pts[0], [bx, by] = pts[pts.length - 1];
      const L = Math.hypot(bx - ax, by - ay);
      if (L > 0.5) {
        const ux = (bx - ax) / L, uy = (by - ay) / L;
        const x1 = x + ux * (r + 4), y1 = y + uy * (r + 4), x2 = x + ux * (r + 24), y2 = y + uy * (r + 24);
        const hx = x2 - ux * 9, hy = y2 - uy * 9, nx = -uy * 6, ny = ux * 6;
        const arrow = `M${x1},${y1} L${x2},${y2} M${hx + nx},${hy + ny} L${x2},${y2} L${hx - nx},${hy - ny}`;
        over += `<path d="${arrow}" ${halo} stroke-width="${w + 3}" opacity="${op}"/>` +
          `<path d="${arrow}" fill="none" stroke="${col}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" opacity="${op}"/>`;
      }
    }
    if (d.id === state.detSel) {
      over += `<circle cx="${x}" cy="${y}" r="${r + 4}" fill="none" stroke="${col}" stroke-width="2">` +
        `<animate attributeName="r" values="${r + 4};${r + 20};${r + 4}" dur="1.8s" repeatCount="indefinite"/>` +
        `<animate attributeName="opacity" values="0.9;0;0.9" dur="1.8s" repeatCount="indefinite"/></circle>`;
    }
    // tag: number + kind symbol, where it overlaps nothing
    const text = `${d.id}`, glyph = KIND_GLYPH[d.kind] || "";
    const tw = 16 + text.length * 12 + (glyph ? 19 : 0), th = 27;
    const cands = [[x + r + 5, y - r - th + 6], [x - r - 5 - tw, y - r - th + 6], [x + r + 5, y + r - 6], [x - r - 5 - tw, y + r - 6]];
    const fits = ([lx, ly]) => lx >= 4 && ly >= 4 && lx + tw <= 996 && ly + th <= 996;
    const free = ([lx, ly]) => !placed.some((q) => lx < q[0] + q[2] && lx + tw > q[0] && ly < q[1] + q[3] && ly + th > q[1]) &&
      !visible.some((v) => v.d !== d && Math.abs(v.x - (lx + tw / 2)) < tw / 2 + 18 && Math.abs(v.y - (ly + th / 2)) < th / 2 + 18);
    const [lx, ly] = cands.find((c) => fits(c) && free(c)) || cands.find(fits) || cands[0];
    placed.push([lx, ly, tw, th]);
    over += `<g opacity="${dim}"><rect x="${lx}" y="${ly}" width="${tw}" height="${th}" rx="8" fill="#080c1c" fill-opacity="0.85" ` +
      `stroke="${col}" stroke-width="${on ? 2 : 1.2}"/>` +
      `<text x="${lx + 8}" y="${ly + 19.5}" font-size="18" font-weight="700" fill="#fff">${text}` +
      (glyph ? `<tspan dx="4" fill="${col}">${glyph}</tspan>` : "") + `</text></g>`;
    state.marks.push({ x, y, kind: "det", s: d });
  }
  return under + over;
}

function detPosAt(d, it) {
  if (!it || it.mjd === undefined || it.mjd === null) return null;
  if (d.kind === "fast" && d.epoch !== it.epoch) return null;          // asteroids: only in their epoch
  const pts = d.points;
  if (pts.length === 1) return pts[0];
  let a = pts[0], b = pts[1];
  for (let i = 1; i < pts.length; i++) { a = pts[i - 1]; b = pts[i]; if (it.mjd <= b.mjd) break; }
  const k = b.mjd === a.mjd ? 0 : (it.mjd - a.mjd) / (b.mjd - a.mjd);
  return { ra: a.ra + k * (b.ra - a.ra), dec: a.dec + k * (b.dec - a.dec) };
}

function detTitle(d) {
  return { moving: "Moving object", fast: "Fast mover (asteroid/comet?)", brightening: "Got brighter", fading: "Got fainter" }[d.kind];
}

function detDetails(d) {
  const bits = [];
  if (d.pm_arcsec_yr) bits.push(`${d.pm_arcsec_yr}″/yr toward PA ${d.pa_deg}°`);
  if (d.rate_arcsec_hr) bits.push(`${d.rate_arcsec_hr}″/hour on ${fmtSpan(d.points[0].mjd, d.points[d.points.length - 1].mjd)}` +
    (showEpochs() && d.epoch !== undefined ? ` (epoch ${d.epoch + 1})` : ""));
  if (d.change_pct !== undefined) bits.push(`${d.change_pct > 0 ? "+" : ""}${d.change_pct}% flux`);
  bits.push(`SNR ${d.snr}`, `${d.points.length} position(s), confirmed by ${d.support} ` +
    (showEpochs() ? "independent epoch pair(s)" : "date comparison(s)"));
  const m = d.matches || {};
  const ids = [m.gaia ? `Gaia DR3 ${esc(m.gaia.source_id)}` : "", m.allwise ? esc(m.allwise.name) : ""].filter(Boolean).join(" · ");
  return `<br>${bits.join(" · ")}<br><span class="${esc(d.verdict_class || "inconclusive")}">${esc(d.verdict)}</span>` +
    (ids ? `<br><span class="dim">${ids}</span>` : "");
}

function renderDetList() {
  const list = $("det-list");
  list.innerHTML = "";
  for (const d of state.dets) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = `${state.detSel === d.id ? "on " : ""}v-${d.verdict_class}`;
    const cls = { moving: "new", fast: "fast", brightening: "var", fading: "var" }[d.kind];
    b.innerHTML = `<span class="kind ${cls}">#${d.id} ${detTitle(d)}</span>${detDetails(d)}`;
    b.addEventListener("click", () => selectDet(d));
    li.appendChild(b);
    list.appendChild(li);
  }
}

function selectDet(d) {
  state.detSel = d.id;
  renderDetList();
  if (d.kind === "fast" && state.view === "frames") {                   // jump into its epoch
    const i = state.items.findIndex((it) => it.epoch === d.epoch);
    if (i >= 0) { stop(); show(i); }
  }
  const it = state.items[state.idx];
  const at = detPosAt(d, it), p = at || d.points[d.points.length - 1];
  const year = at && Number.isFinite(it.t) ? it.t : jyear(p.mjd);     // the date p belongs to
  let pm = null;                       // a slow mover: follow its measured motion (or Gaia's, if matched)
  const g = d.matches && d.matches.gaia;
  if (g && Number.isFinite(g.pmra) && Number.isFinite(g.pmdec)) pm = { pmra: g.pmra, pmdec: g.pmdec, year };
  else if (d.kind === "moving" && d.points.length > 1) {
    const a = d.points[0], b = d.points[d.points.length - 1], dy = (b.mjd - a.mjd) / 365.25;
    if (dy > 0) pm = { pmra: (b.ra - a.ra) * Math.cos(a.dec * Math.PI / 180) * 3.6e6 / dy, pmdec: (b.dec - a.dec) * 3.6e6 / dy, year };
  }
  state.click = { ra: p.ra, dec: p.dec, label: `#${d.id} ${detTitle(d).toLowerCase()}`, pm };
  drawOverlay();
  loadSpectrum(state.click);
}

// ------------------------------------------------------------- export report --
function csvCell(v) {
  if (v === null || v === undefined) return "";
  const t = String(v);
  return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

function detToCSV(res) {
  const p = res.provenance, c = p.catalogs;
  const head = [
    `# ${p.tool} ${p.version} — automatic detection report`,
    `# run_utc: ${p.run_utc}`,
    `# target: RA ${p.target.ra_deg} Dec ${p.target.dec_deg} deg, field ${p.target.fov_deg} deg, detector ${p.target.detector}, ` +
      `${p.target.grid_px} px of ${p.target.pixel_arcsec} arcsec`,
    `# threshold: ${p.parameters.sigma} sigma; wavelength window: ${p.parameters.lam_min_um ?? 0.75}-${p.parameters.lam_max_um ?? 5.0} um; ` +
      `same-wavelength tolerance: ${p.parameters.match_dlam_um} um`,
    `# data: ${p.data.archive}, releases ${p.data.releases.join(" ")}, ${p.data.n_frames} frames (frame list in the JSON report)`,
    `# catalogs: Gaia ${c.gaia.via || "unavailable"}; AllWISE ${c.allwise.via || "unavailable"}; ` +
      `asteroids ${c.asteroids.service} (${c.asteroids.queries} queries${c.asteroids.error ? ", error" : ""})`,
  ];
  const cols = ["id", "kind", "verdict_class", "verdict", "reasons", "snr", "support", "n_points",
    "first_mjd", "last_mjd", "ra_first_deg", "dec_first_deg", "ra_last_deg", "dec_last_deg",
    "pm_arcsec_yr", "pa_deg", "rate_arcsec_hr", "change_pct", "gaia_source_id", "allwise_name", "asteroid"];
  const rows = res.candidates.map((d) => {
    const a = d.points[0], b = d.points[d.points.length - 1], m = d.matches || {};
    return [d.id, d.kind, d.verdict_class, d.verdict, (d.reasons || []).join("; "), d.snr, d.support, d.points.length,
      a.mjd.toFixed(5), b.mjd.toFixed(5), a.ra.toFixed(6), a.dec.toFixed(6), b.ra.toFixed(6), b.dec.toFixed(6),
      d.pm_arcsec_yr, d.pa_deg, d.rate_arcsec_hr, d.change_pct, m.gaia && m.gaia.source_id,
      m.allwise && m.allwise.name, m.asteroid && `${m.asteroid.number ? `(${m.asteroid.number}) ` : ""}${m.asteroid.name}`,
    ].map(csvCell).join(",");
  });
  return head.join("\n") + "\n" + cols.join(",") + "\n" + rows.join("\n") + "\n";
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Self-contained, versioned, machine-readable package (built by the server from the result shown).
async function downloadPackage() {
  if (!state.detResult) return;
  const btn = $("det-pkg");
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/package`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.detResult),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const m = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "");
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = m ? m[1] : reportName("zip");
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    $("det-status").textContent = `Package failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function reportName(ext) {
  return `spherex_detections_${state.ra.toFixed(4)}_${state.dec.toFixed(4)}_${new Date().toISOString().slice(0, 10)}.${ext}`;
}

async function runDetect() {
  if (state.ra === null || !state.frames.length) {
    $("det-status").textContent = "Load a target first (Show the sky).";
    return;
  }
  const token = state.token;
  const base = "Comparing observation dates at matching wavelengths, checking Gaia, AllWISE and SkyBoT…";
  $("det-status").textContent = base;
  $("det-run").disabled = true;
  const started = Date.now();
  const tick = setInterval(() => {                    // show that it is still working
    if (token !== state.token) { clearInterval(tick); return; }
    const sec = Math.round((Date.now() - started) / 1000);
    $("det-status").textContent = `${base} ${sec} s` +
      (sec >= 15 ? " — still working. Catalogs that do not answer within ~25 s are skipped " +
        "(the result then says so); the frame comparison itself always finishes." : "");
  }, 1000);
  try {
    const res = await getJSON(`/api/detect?${qs({
      ra: state.ra, dec: state.dec, fov: state.fov, det: state.det, sigma: $("det-sigma").value, gaia: 1, ...lamParams(), ...rangeParams(),
      ...covParam(), ...fastParam(),
    })}`, channel("detect"));
    if (token !== state.token) return;
    state.dets = res.candidates;
    state.detResult = res;
    state.detSel = null;
    const nm = res.candidates.filter((c) => c.kind === "moving").length;
    const nf = res.candidates.filter((c) => c.kind === "fast").length;
    const nv = res.candidates.length - nm - nf;
    const sm = res.summary || {};
    $("det-status").textContent = `${res.n_frames} frames, ${(res.date_pairs || res.epoch_pairs).length} ` +
      `${showEpochs() ? "epoch pair(s) compared" : "date comparison(s)"} at ${res.sigma}σ: ` +
      `${nm} moving, ${nf} fast, ${nv} brightness change(s) → ${sm.unmatched || 0} unmatched, ` +
      `${sm.known || 0} known, ${sm.inconclusive || 0} inconclusive` +
      (state.loading ? ". More frames are still loading — run again when done." : ".");
    const cats = (res.provenance && res.provenance.catalogs) || {};
    const late = [["Gaia", cats.gaia], ["AllWISE", cats.allwise], ["SkyBoT", cats.asteroids]]
      .filter(([, c]) => c && c.error).map(([n, c]) => (/still loading/.test(c.error) ? `${n} is still loading` : `${n} unavailable`));
    if (late.length) {
      $("det-status").textContent += ` ${late.join(", ")} — those checks are marked inconclusive; ` +
        "press Find again in a minute to include them.";
    }
    $("det-export").hidden = !res.candidates.length;
    if (!res.candidates.length) $("det-status").textContent += " Nothing above the noise — try Sensitive, or Detector All.";
    renderDetList();
    drawOverlay();
  } catch (err) {
    if (isAbort(err)) { if (token === state.token) $("det-status").textContent = ""; }
    else if (token === state.token) $("det-status").textContent = `Search failed: ${err.message}`;
  } finally {
    clearInterval(tick);
    $("det-run").disabled = false;
  }
}

// --------------------------------------------------------- hover tooltip --
function tipHtml(m) {
  const s = m.s, pm = Math.hypot(s.pmra, s.pmdec);
  const pmLine = `PM (${Math.round(s.pmra)}, ${Math.round(s.pmdec)}) mas/yr` +
    (pm >= 100 ? ` · ${(pm / 1000).toFixed(2)}″/yr` : "");
  if (m.kind === "det") return `<b class="det">#${s.id} ${detTitle(s)}</b>${detDetails(s)}`;
  if (m.kind === "gaia") {
    const dist = s.plx && s.plx > 0 ? ` · ~${(1000 / s.plx).toFixed(s.plx > 100 ? 2 : 0)} pc` : "";
    return `<b class="gaia">${s.source_id ? `Gaia DR3 ${esc(s.source_id)}` : "Gaia DR3 star"}</b>` +
      `G ${s.g === null ? "—" : s.g.toFixed(2)}` +
      (s.plx !== undefined && s.plx !== null ? ` · parallax ${s.plx.toFixed(1)} mas${dist}` : "") +
      `<br>${pmLine}<br><span class="dim">Known to Gaia</span>`;
  }
  return `<b class="wise">${esc(s.name)}</b>` +
    `W1 ${s.w1 === null ? "—" : s.w1.toFixed(2)} · W2 ${s.w2 === null ? "—" : s.w2.toFixed(2)}` +
    (s.w1 !== null && s.w2 !== null ? ` · W1−W2 ${(s.w1 - s.w2).toFixed(2)}` : "") +
    `<br>${pmLine}<br><span class="dim">AllWISE (positions 2010, moved to this frame's date)</span>`;
}

// Stable identity of a marker: Gaia source id, AllWISE name, or detection number.
const markKey = (m) => `${m.kind}:${m.s.source_id ?? m.s.name ?? m.s.id}`;

function hideTip() {
  $("hover-tip").hidden = true;
  if (state.hover) { state.hover = null; }
}

$("stage").addEventListener("mousemove", (e) => {
  if (!state.marks.length) { hideTip(); return; }
  const r = e.currentTarget.getBoundingClientRect();
  const fx = (e.clientX - r.left) / r.width * 1000, fy = (e.clientY - r.top) / r.height * 1000;
  const limit = 16 / r.width * 1000;                    // within ~16 screen pixels
  let best = null, bd = Infinity;
  for (const m of state.marks) {
    const d = Math.hypot(m.x - fx, m.y - fy);
    if (d < bd) { bd = d; best = m; }
  }
  if (!best || bd > limit) {
    if (state.hover) { hideTip(); drawOverlay(); }
    return;
  }
  const tip = $("hover-tip");
  tip.innerHTML = tipHtml(best);
  tip.hidden = false;
  const px = e.clientX - r.left, py = e.clientY - r.top;
  tip.style.left = `${Math.min(px + 14, r.width - tip.offsetWidth - 6)}px`;
  tip.style.top = `${py + 14 + tip.offsetHeight > r.height ? py - tip.offsetHeight - 10 : py + 14}px`;
  const changed = !state.hover || markKey(state.hover) !== markKey(best);
  state.hover = best;
  if (changed) drawOverlay();
});
$("stage").addEventListener("mouseleave", () => { if (state.hover) { hideTip(); drawOverlay(); } });

// ----------------------------------------------------------------- spectrum --
// Where a clicked point is on a given date: fixed sky point, or a star that moves (proper motion).
function clickAt(c, year) {
  if (!c || !c.pm || !Number.isFinite(year)) return c;
  const dt = year - c.pm.year;
  return {
    ...c,
    ra: c.ra + (c.pm.pmra * dt / 3.6e6) / Math.cos(c.dec * Math.PI / 180),
    dec: c.dec + c.pm.pmdec * dt / 3.6e6,
  };
}

// A click near a Gaia star (or the target itself, if it moves) measures THAT star in every frame,
// following its motion — otherwise a fast star like Barnard's walks out of the aperture over a year.
function pickPoint(sky, it) {
  const year = it && Number.isFinite(it.t) ? it.t : null;
  const near = 2.5 * PIX_DEG;                                   // within 2.5 SPHEREx pixels
  const dist = (a) => Math.hypot((a.ra - sky.ra) * Math.cos(sky.dec * Math.PI / 180), a.dec - sky.dec);
  if (year !== null && state.gaiaBase) {
    let best = null, bd = near;
    for (const s of catAt(state.gaiaBase, year)) { const d = dist(s); if (d < bd) { bd = d; best = s; } }
    if (best) {
      const pm = Math.hypot(best.pmra, best.pmdec);
      return { ra: best.ra, dec: best.dec, label: `Gaia DR3 ${best.source_id}`,
        pm: pm > 20 ? { pmra: best.pmra, pmdec: best.pmdec, year } : null };
    }
  }
  if (year !== null && state.targetPm && Math.hypot(state.targetPm.pmra, state.targetPm.pmdec) > 20) {
    const t = clickAt({ ra: state.ra, dec: state.dec, pm: state.targetPm }, year);
    if (dist(t) < near) return { ra: t.ra, dec: t.dec, label: state.targetPm.name, pm: { ...state.targetPm, year } };
  }
  return { ra: sky.ra, dec: sky.dec, label: null, pm: null };
}

async function loadSpectrum(c) {
  if (!c) return;
  const token = state.token;
  $("spec-note").textContent = c.label ? `Measuring ${c.label} in every frame…` : "Measuring this point in every frame…";
  try {
    const res = await getJSON(`/api/spectrum?${qs({
      ra: state.ra, dec: state.dec, fov: state.fov, x_ra: c.ra.toFixed(6), x_dec: c.dec.toFixed(6),
      ...(c.pm ? { pmra: c.pm.pmra.toFixed(2), pmdec: c.pm.pmdec.toFixed(2), pm_year: c.pm.year.toFixed(4) } : {}),
    })}`, channel("spectrum"));
    if (token !== state.token || state.click !== c) return;      // another click / target meanwhile
    state.specPoints = res.points;
    drawChart();
    const sk = res.skipped || {};
    const pm = c.pm ? Math.hypot(c.pm.pmra, c.pm.pmdec) / 1000 : 0;
    const why = res.points.length ? "" :
      (sk["not downloaded"] && !sk["outside frame"] ? " No frames are downloaded yet — wait until they load." :
        " This point is not covered by the downloaded frames.");
    $("spec-note").textContent =
      `${c.label ? `${c.label} · ` : ""}RA ${c.ra.toFixed(4)}°, Dec ${c.dec.toFixed(4)}° · ${res.points.length} measurements` +
      (c.pm ? ` · the aperture follows its motion (${pm.toFixed(pm < 1 ? 2 : 1)}″/yr)` : "") + "." + why +
      (res.points.length ? " Click a dot to jump to that frame. Tip: detector All (D1–D6) gives the full 0.75–5 µm range." : "") +
      (state.loading && res.points.length ? " More frames are loading — the chart updates when they are done." : "");
  } catch (err) {
    if (!isAbort(err) && token === state.token) $("spec-note").textContent = `Spectrum unavailable: ${err.message}`;
  }
}

function niceTicks(lo, hi, count = 5) {
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function drawChart() {
  const svg = $("spectrum");
  const legend = $("spec-legend");
  const lc = state.chart === "lightcurve";
  const W = 960, H = 360, L = 78, R = 24, T = 20, B = 56;
  const pts = (state.specPoints || []).filter((p) =>
    Number.isFinite(p.lam_um) && Number.isFinite(p.flux) && Number.isFinite(p.mjd) && inRange(p));
  if (!pts.length) {
    legend.innerHTML = "";
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" fill="#9AA3B8" font-size="18" text-anchor="middle">` +
      `${state.specPoints ? "No measurements for this point" : "Click a point in the image to measure it"}</text>`;
    return;
  }
  const xv = (p) => (lc ? p.mjd : p.lam_um);
  const colorOf = (p) => (lc ? DET_COLORS[p.det] || "#C9CFDD" : EPOCH_COLORS[p.epoch % EPOCH_COLORS.length]);
  const xs = pts.map(xv), fl = pts.map((p) => p.flux);
  let xmin, xmax;
  if (lc) { const pad = Math.max(5, (Math.max(...xs) - Math.min(...xs)) * 0.04); xmin = Math.min(...xs) - pad; xmax = Math.max(...xs) + pad; }
  else { xmin = Math.max(0.7, Math.min(...xs) - 0.1); xmax = Math.min(5.1, Math.max(...xs) + 0.1); }
  let ymin = Math.min(...fl), ymax = Math.max(...fl);
  if (ymax === ymin) { ymax += 1; ymin -= 1; }
  const pad = (ymax - ymin) * 0.08; ymin -= pad; ymax += pad;
  const X = (v) => L + ((v - xmin) / (xmax - xmin)) * (W - L - R);
  const Y = (v) => H - B - ((v - ymin) / (ymax - ymin)) * (H - T - B);

  let s = "";
  for (const t of niceTicks(ymin, ymax, 5)) {
    s += `<line x1="${L}" y1="${Y(t)}" x2="${W - R}" y2="${Y(t)}" stroke="#1E2740"/>` +
      `<text x="${L - 10}" y="${Y(t) + 5}" fill="#9AA3B8" font-size="14" text-anchor="end">${t}</text>`;
  }
  if (ymin < 0 && ymax > 0) {
    s += `<line x1="${L}" y1="${Y(0)}" x2="${W - R}" y2="${Y(0)}" stroke="#2C3755" stroke-dasharray="4 4"/>`;
  }
  const xticks = lc ? niceTicks(xmin, xmax, 6) : niceTicks(xmin, xmax, 8);
  for (const t of xticks) {
    const label = lc ? mjdToISO(t).slice(0, 10) : t;
    s += `<line x1="${X(t)}" y1="${H - B}" x2="${X(t)}" y2="${H - B + 6}" stroke="#2C3755"/>` +
      `<text x="${X(t)}" y="${H - B + 24}" fill="#9AA3B8" font-size="14" text-anchor="middle">${label}</text>`;
  }
  s += `<line x1="${L}" y1="${T}" x2="${L}" y2="${H - B}" stroke="#2C3755"/>` +
    `<line x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}" stroke="#2C3755"/>` +
    `<text x="${(L + W - R) / 2}" y="${H - 8}" fill="#C9CFDD" font-size="15" text-anchor="middle">${lc ? "Date of observation" : "Wavelength, µm"}</text>` +
    `<text x="18" y="${(T + H - B) / 2}" fill="#C9CFDD" font-size="15" text-anchor="middle" transform="rotate(-90 18 ${(T + H - B) / 2})">Brightness, MJy/sr</text>`;

  if (!lc && !lamIsFull()) {                              // the wavelength window on screen
    const x0 = X(Math.max(xmin, state.lam[0])), x1 = X(Math.min(xmax, state.lam[1]));
    if (x1 > x0) {
      s += `<rect x="${x0}" y="${T}" width="${x1 - x0}" height="${H - B - T}" fill="#F5B342" opacity="0.10"/>` +
        `<text x="${(x0 + x1) / 2}" y="${T + 16}" fill="#F5B342" font-size="13" text-anchor="middle">window on screen</text>`;
    }
  }
  // faint connecting lines: per epoch (spectrum) or per detector (light curve)
  const groupKey = (p) => (lc ? p.det : p.epoch);
  const groups = [...new Set(pts.map(groupKey))].sort();
  for (const g of groups) {
    const gp = pts.filter((p) => groupKey(p) === g).sort((a, b) => xv(a) - xv(b));
    const line = gp.map((p) => `${X(xv(p)).toFixed(1)},${Y(p.flux).toFixed(1)}`).join(" ");
    s += `<polyline points="${line}" fill="none" stroke="${colorOf(gp[0])}" stroke-width="1.5" opacity="0.35"/>`;
  }
  for (const p of pts) {
    s += `<circle class="pt" data-id="${p.id || ""}" cx="${X(xv(p))}" cy="${Y(p.flux)}" r="5" fill="${colorOf(p)}">` +
      `<title>${p.lam_um.toFixed(3)} µm · ${p.flux.toPrecision(3)} MJy/sr · ${p.date.slice(0, 10)} · ${p.det}</title></circle>`;
  }
  svg.innerHTML = s;
  legend.innerHTML = lc
    ? groups.map((d) => `<span><i style="background:${DET_COLORS[d]}"></i>${d}</span>`).join("") +
      `<span class="muted">Compare dots of one color: each detector covers a different wavelength range.</span>`
    : groups.map((e) => `<span><i style="background:${EPOCH_COLORS[e % EPOCH_COLORS.length]}"></i>${epochTag(e)}${epochDate(e)}</span>`).join("");
}

// ------------------------------------------------------------------- events --
function setViewButtons() {
  document.querySelectorAll("[data-view]").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === state.view));
}

$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.name = "";
  load();
});

document.querySelectorAll("[data-chart]").forEach((b) =>
  b.addEventListener("click", () => {
    state.chart = b.dataset.chart;
    document.querySelectorAll("[data-chart]").forEach((x) => x.classList.toggle("on", x === b));
    drawChart();
  }));

document.querySelectorAll("[data-view]").forEach((b) =>
  b.addEventListener("click", () => {
    stop();
    state.view = b.dataset.view;
    setViewButtons();
    if (state.frames.length) { buildSequence(); writeURL(); }
  }));

$("play").addEventListener("click", () => (state.playing ? stop() : play()));
$("prev").addEventListener("click", () => { stop(); show(state.idx - 1); });
$("next").addEventListener("click", () => { stop(); show(state.idx + 1); });
// Look of the image: instant, no new requests to the server.
function applyLook() {
  const sp = parseFloat($("speed").value), b = $("bright").value, c = $("contrast").value;
  $("speed-val").textContent = `${sp} fps`;
  $("bright-val").textContent = `${b}%`;
  $("contrast-val").textContent = `${c}%`;
  $("frame-img").style.filter = `brightness(${b / 100}) contrast(${c / 100})`;
}
$("speed").addEventListener("input", () => { applyLook(); if (state.playing) play(); });
$("bright").addEventListener("input", applyLook);
$("contrast").addEventListener("input", applyLook);
["speed", "bright", "contrast"].forEach((id) =>
  $(id).addEventListener("change", () => state.ra !== null && writeURL()));
$("reset-look").addEventListener("click", () => {
  $("speed").value = 2; $("bright").value = 100; $("contrast").value = 100;
  applyLook(); if (state.playing) play(); if (state.ra !== null) writeURL();
});
$("flags-toggle").addEventListener("change", () => {
  updateFrameExtras(state.items[state.idx]);
  if (state.ra !== null) writeURL();
});

// ------------------------------------------------------------ epochs on / off --
function applyEpochToggle(rebuild) {
  const on = showEpochs();
  const btn = document.querySelector('[data-view="coadd"]');
  if (btn) btn.textContent = on ? "Co-add by epoch" : "Stack by date";
  $("m-epoch").hidden = $("m-epoch-dt").hidden = !on;
  if (!rebuild) return;
  renderEpochChips();
  if (state.items.length) { const i = state.idx; buildSequence(i); }
  drawChart();
  if (state.dets.length) renderDetList();
  if (state.ra !== null) writeURL();
}

// --------------------------------------------------------------- date window --
function readRange() {
  let a = $("date-from").value, b = $("date-to").value;
  if (a && b && a > b) { [a, b] = [b, a]; $("date-from").value = a; $("date-to").value = b; }   // swapped by mistake
  state.range = [a ? dateToMjd(a) : null, b ? dateToMjd(b) + 1 : null];                          // "to" is inclusive
}

const covers = (loaded, want) => loaded &&
  (loaded[0] === null || (want[0] !== null && want[0] >= loaded[0])) &&
  (loaded[1] === null || (want[1] !== null && want[1] <= loaded[1]));

function renderEpochChips() {
  const box = $("epoch-chips");
  const det = $("det").value;
  const fs = state.searchFrames.filter((f) => det === "all" || f.det === det);
  if (!fs.length) { box.hidden = true; return; }
  const by = {};
  for (const f of fs) (by[f.epoch] = by[f.epoch] || []).push(f.mjd);
  const span = (ms) => [Math.min(...ms), Math.max(...ms)];
  const all = span(fs.map((f) => f.mjd));
  $("date-from").min = $("date-to").min = mjdToDate(all[0]);
  $("date-from").max = $("date-to").max = mjdToDate(all[1]);
  const chips = [{ label: `All dates · ${fs.length} frames`, from: "", to: "" }].concat(
    Object.keys(by).map(Number).sort((a, b) => a - b).map((e) => {
      const [lo, hi] = span(by[e]);
      return { label: `${epochTag(e)}${fmtSpan(lo, hi)} · ${by[e].length} frames`, from: mjdToDate(lo), to: mjdToDate(hi) };
    }));
  box.innerHTML = '<span class="lbl">SPHEREx observed this point:</span>';
  for (const c of chips) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.label;
    b.className = $("date-from").value === c.from && $("date-to").value === c.to ? "on" : "";
    b.addEventListener("click", () => { $("date-from").value = c.from; $("date-to").value = c.to; onRangeChange(); });
    box.appendChild(b);
  }
  box.hidden = false;
}

function onRangeChange() {
  readRange();
  renderEpochChips();
  if (state.ra === null) return;
  writeURL();
  if (!state.frames.length && !state.loading) return;
  if (!covers(state.loadedRange, state.range)) { load(); return; }     // need frames that were not downloaded
  state.dets = []; state.detSel = null; state.detResult = null;         // old results were for other dates
  $("det-list").innerHTML = ""; $("det-export").hidden = true;
  $("det-status").textContent = "Date range changed — press Find again.";
  const cur = state.items[state.idx];
  const vis = state.frames.filter(inLam).filter(inRange);
  stop();
  buildSequence(state.view === "frames" && cur && cur.id ? Math.max(0, vis.findIndex((f) => f.id === cur.id)) : 0);
  drawChart();
  if (state.items.length) {
    setStatus(`${vis.length} frames between ` +
      `${$("date-from").value || "the first observation"} and ${$("date-to").value || "the latest"}.`);
  }
}

// ---------------------------------------------------------- wavelength window --
function setLam(lo, hi, rebuild = true) {
  lo = Math.max(LAM_FULL[0], Math.min(lo, LAM_FULL[1]));
  hi = Math.max(LAM_FULL[0], Math.min(hi, LAM_FULL[1]));
  if (!(hi > lo)) {                                    // invalid input: put the last valid window back
    $("lam-min").value = state.lam[0]; $("lam-max").value = state.lam[1];
    return;
  }
  state.lam = [+lo.toFixed(2), +hi.toFixed(2)];
  $("lam-min").value = state.lam[0];
  $("lam-max").value = state.lam[1];
  const preset = [...$("lam-preset").options].find((o) => o.value === `${lo},${hi}` ||
    o.value.split(",").map(Number).join() === state.lam.join());
  $("lam-preset").value = preset ? preset.value : "custom";
  if (!rebuild) return;
  if (state.frames.length) {
    const cur = state.items[state.idx];
    const visible = state.frames.filter(inLam);
    const keep = state.view === "frames" && cur && cur.id ? Math.max(0, visible.findIndex((f) => f.id === cur.id)) : 0;
    stop(); buildSequence(keep);
    if (state.view === "rgb") setStatus(`Color always uses three fixed ranges (${RGB_BANDS}); ` +
      "the window applies to Frames, Stack by date and Clean difference.");
    else if (state.items.length) setStatus(`${state.items.length} ${state.view === "frames" ? "frames" : "images"} in ` +
      `${state.lam[0]}–${state.lam[1]} µm. Press Play.`);
  }
  drawChart();
  if (state.ra !== null) writeURL();
}
$("lam-preset").addEventListener("change", () => {
  const v = $("lam-preset").value;
  if (v === "custom") { $("lam-min").focus(); return; }
  const [lo, hi] = v.split(",").map(Number);
  setLam(lo, hi);
});
["lam-min", "lam-max"].forEach((id) => $(id).addEventListener("change", () =>
  setLam(parseFloat($("lam-min").value), parseFloat($("lam-max").value))));

$("det-run").addEventListener("click", runDetect);
$("epoch-toggle").addEventListener("change", () => applyEpochToggle(true));
$("set-cov").addEventListener("change", () => {           // other frames qualify -> load again (cached, fast)
  if (state.ra !== null) load(); else writeURL();
});
$("set-fast").addEventListener("change", () => {
  if (state.dets.length) {
    state.dets = []; state.detSel = null; state.detResult = null;
    $("det-list").innerHTML = ""; $("det-export").hidden = true; drawOverlay();
    $("det-status").textContent = "Setting changed — press Find again.";
  }
  if (state.ra !== null) writeURL();
});
$("set-fresh").addEventListener("click", async () => {
  if (state.ra === null) { $("set-fresh-note").textContent = "Load a target first."; return; }
  const btn = $("set-fresh");
  btn.disabled = true;
  $("set-fresh-note").textContent = "Asking IRSA for the latest frame list…";
  try {
    const before = state.searchFrames.length;
    const found = await searchUntilDone({ ra: state.ra, dec: state.dec, fresh: 1 }, undefined, "fresh");
    const added = found.n_frames - before;
    $("set-fresh-note").textContent = added > 0 ? `${added} new frame(s) found — loading them.` : "No new frames since the last check.";
    if (added > 0) load();
  } catch (err) {
    if (!isAbort(err)) $("set-fresh-note").textContent = `IRSA check failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});
getJSON("/api/health", null).then((h) => {
  $("set-server").textContent = `Server: Gaia table ${h.gaia_table} · cache limit ${h.cache_limit_gb} GB · ` +
    `${h.irsa_concurrency} parallel IRSA downloads. Changed only by whoever runs the server.`;
}).catch(() => {});
["date-from", "date-to"].forEach((id) => $(id).addEventListener("change", onRangeChange));
$("det").addEventListener("change", renderEpochChips);
$("det-csv").addEventListener("click", () => state.detResult &&
  download(reportName("csv"), detToCSV(state.detResult), "text/csv"));
$("det-json").addEventListener("click", () => state.detResult &&
  download(reportName("json"), JSON.stringify(state.detResult, null, 2), "application/json"));
$("det-pkg").addEventListener("click", downloadPackage);
$("det-show").addEventListener("change", drawOverlay);
$("smooth-toggle").addEventListener("change", () => {
  if (state.frames.length) { const i = state.idx; buildSequence(i); }
});

$("wise-toggle").addEventListener("change", async () => {
  if ($("wise-toggle").checked) await loadWise(); else { drawOverlay(); $("wise-status").textContent = ""; }
  if (state.ra !== null) writeURL();
});

$("gaia-depth").addEventListener("change", async () => {
  state.gaiaBase = null;
  if ($("gaia-toggle").checked) await loadGaia();
});

$("gaia-toggle").addEventListener("change", async () => {
  if ($("gaia-toggle").checked) await loadGaia(); else { drawOverlay(); $("gaia-status").textContent = ""; }
  if (state.ra !== null) writeURL();
});

$("stage").addEventListener("click", (e) => {
  if (state.ra === null || !state.items.length) return;
  const r = e.currentTarget.getBoundingClientRect();
  const sky = fracToSky((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
  state.click = pickPoint(sky, state.items[state.idx]);
  drawOverlay();
  loadSpectrum(state.click);
});

$("spectrum").addEventListener("click", (e) => {
  const id = e.target.dataset && e.target.dataset.id;
  if (!id) return;
  if (state.view !== "frames") { state.view = "frames"; setViewButtons(); buildSequence(); }
  const i = state.items.findIndex((it) => it.id === id);
  if (i >= 0) { stop(); show(i); $("stage").scrollIntoView({ behavior: "smooth", block: "center" }); }
});

$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("share-url").value); $("copy").textContent = "Copied"; }
  catch (_) { $("share-url").select(); }
  setTimeout(() => ($("copy").textContent = "Copy"), 1500);
});

document.querySelectorAll("[data-target]").forEach((b) =>
  b.addEventListener("click", () => {
    $("target").value = b.dataset.target;
    if (b.dataset.det) $("det").value = b.dataset.det;
    state.name = b.dataset.name || "";
    document.getElementById("viewer").scrollIntoView();
    load();
  }));

document.addEventListener("keydown", (e) => {
  const tag = document.activeElement ? document.activeElement.tagName : "";
  if (["INPUT", "SELECT", "TEXTAREA"].includes(tag)) return;
  if (e.key === " " && (tag === "BUTTON" || tag === "A")) return;   // native click of that button
  if (e.key === "ArrowRight") { stop(); show(state.idx + 1); }
  else if (e.key === "ArrowLeft") { stop(); show(state.idx - 1); }
  else if (e.key === " ") { e.preventDefault(); state.playing ? stop() : play(); }
});

// nav highlight
const navLinks = [...document.querySelectorAll(".nav a")];
const observer = new IntersectionObserver((entries) => {
  entries.forEach((en) => {
    if (en.isIntersecting) {
      navLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${en.target.id}`));
    }
  });
}, { rootMargin: "-40% 0px -55% 0px" });
document.querySelectorAll("main > section").forEach((s) => observer.observe(s));

// start
drawChart();
if (readURL()) load();
