// App wiring: connects state, timeline, navbar, deck, audio, attribution.
import { api } from "./api.js";
import * as state from "./state.js";
import * as align from "./align.js";
import * as nav from "./navbar.js";
import { rankRecommendations, scorePercent, piePath } from "./deck.js";
import * as boot from "./boot.js";
import { attributionParts, licenseBadges } from "./attribution.js";
import { Timeline, COLORS, trackColor } from "./timeline.js";
import { Player } from "./audio.js";

const $ = (sel) => document.querySelector(sel);

const mix = state.createMix();
const vp = nav.createViewport(60);
const timeline = new Timeline($("#timeline"), vp);
timeline.setMix(mix);
const player = new Player();

let catalog = [];
let currentMarkers = [];
let currentTransition = null;
let gridBpm = null;
// Index of the track being dragged, and the markers/grid that apply to it.
let markerOriginIndex = 0;

// ---------------------------------------------------------------- helpers
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function drawMiniWaveform(canvas, points, color) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = 110 * dpr; canvas.height = 30 * dpr;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = color;
  const n = points.length;
  for (let i = 0; i < n; i++) {
    const x = (i / n) * canvas.width;
    const h = Math.max(1, points[i] * canvas.height * 0.9);
    ctx.fillRect(x, (canvas.height - h) / 2, Math.max(1, canvas.width / n * 0.7), h);
  }
}

function updateTimes() {
  $("#time-total").textContent = state.formatTime(state.totalDuration(mix));
  $("#time-now").textContent = state.formatTime(timeline.cursor);
}

// ------------------------------------------------------------ attribution
function renderAttributions() {
  const el = $("#attributions");
  el.innerHTML = "";
  mix.tracks.forEach((t, idx) => {
    const meta = catalog.find((c) => c.id === t.id);
    if (!meta) return;
    const parts = attributionParts(meta.attribution);
    const span = document.createElement("span");
    const sw = document.createElement("i");
    sw.className = "att-swatch";
    sw.style.background = trackColor(idx);
    span.appendChild(sw);
    span.appendChild(document.createTextNode(parts.text.replace(` \u2014 ${parts.licenseName}`, " \u2014 ")));
    const a = document.createElement("a");
    a.href = parts.licenseUrl;
    a.target = "_blank";
    a.rel = "license noopener";
    a.textContent = parts.licenseName;
    span.appendChild(a);
    el.appendChild(span);
  });
  if (!mix.tracks.length) {
    el.innerHTML = '<span>Attribution for loaded tracks appears here \u2014 all catalog audio is Creative Commons licensed via Jamendo.</span>';
  }
}

// ------------------------------------------------------------------- deck
// Zero state (nothing selected): there is nothing to match against, so the
// deck browses by genre rather than pretending to rank. No pair analysis runs
// until track 1 is chosen.
async function renderDeckZeroState() {
  const { groups, per_genre } = await api.deck();
  $("#deck-sub").textContent =
    `Browse by genre \u2014 top ${per_genre} per genre; pick your opener`;

  const deck = $("#deck");
  deck.innerHTML = "";
  for (const g of groups) {
    const section = document.createElement("section");
    section.className = "genre-group";

    const head = document.createElement("div");
    head.className = "genre-head";
    const name = document.createElement("span");
    name.className = "genre-name";
    name.textContent = g.genre;
    const count = document.createElement("span");
    count.className = "genre-count";
    count.textContent = g.total > g.showing
      ? `${g.showing} of ${g.total}` : `${g.total}`;
    head.append(name, count);

    const list = document.createElement("ol");
    list.className = "deck-list";
    g.tracks.forEach((t) => list.appendChild(deckRow(t, null)));

    section.append(head, list);
    deck.appendChild(section);
  }
}

async function renderDeckRecommendations(forTrackId) {
  const recs = rankRecommendations(await api.recommendations(forTrackId));
  $("#deck-sub").textContent = recs.length
    ? `Suggested next tracks for what\u2019s playing \u2014 ranked by match`
    : "No compatible tracks share a BPM grid with this one.";
  const deck = $("#deck");
  deck.innerHTML = "";
  const list = document.createElement("ol");
  list.className = "deck-list";
  recs.forEach((rec) => {
    // The API inlines the candidate's metadata and waveform, so ranking the
    // deck needs no follow-up request either.
    const meta = rec.track || catalog.find((c) => c.id === rec.track_id);
    if (meta) list.appendChild(deckRow(meta, rec));
  });
  deck.appendChild(list);
}

function deckRow(meta, rec) {
  const li = document.createElement("li");
  li.className = "deck-row";
  const draggable = meta.mixable && state.canAddTrack(mix) && !mix.tracks.some((t) => t.id === meta.id);
  li.setAttribute("draggable", draggable ? "true" : "false");
  li.dataset.trackId = meta.id;

  const wf = document.createElement("canvas");
  li.appendChild(wf);
  // The envelope ships inline with the deck payload — precomputed once at
  // server startup, so a deck row costs no request of its own. This is what
  // removed the boot-time request-per-row fan-out.
  if (meta.waveform) {
    drawMiniWaveform(wf, meta.waveform, trackColor(mix.tracks.length));
  }

  const m = document.createElement("div");
  m.className = "deck-meta";
  m.innerHTML = `<div class="deck-name"></div><div class="deck-artist"></div>`;
  m.querySelector(".deck-name").textContent = meta.name;
  m.querySelector(".deck-artist").textContent =
    `${meta.artist} \u00b7 ${Math.round(meta.bpm)} BPM \u00b7 ${meta.camelot}`;
  li.appendChild(m);

  const tags = document.createElement("div");
  tags.className = "deck-tags";
  for (const b of licenseBadges(meta.license_flags)) {
    const s = document.createElement("span");
    s.className = `tag ${b.code.toLowerCase()}`;
    s.textContent = b.code;
    s.title = b.label;
    tags.appendChild(s);
  }
  li.appendChild(tags);

  const score = document.createElement("div");
  score.className = "deck-score";
  if (rec) {
    const pct = scorePercent(rec.score);
    score.innerHTML =
      `<span>${pct}%</span>` +
      `<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">` +
      `<circle cx="9" cy="9" r="8"></circle><path d="${piePath(rec.score)}"></path></svg>`;
    score.title = `BPM ${Math.round(rec.breakdown.bpm * 100)}% \u00b7 key ${Math.round(rec.breakdown.key * 100)}% \u00b7 energy ${Math.round(rec.breakdown.energy * 100)}%`;
  }
  li.appendChild(score);

  if (draggable) {
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/track-id", meta.id);
      e.dataTransfer.effectAllowed = "copy";
    });
  } else if (!meta.mixable) {
    li.title = "ND-licensed: playback only, cannot be mixed";
  }
  return li;
}

// -------------------------------------------------------------- add tracks
async function addFirstTrack(trackId) {
  const meta = catalog.find((c) => c.id === trackId);
  const res = state.addTrack(mix, {
    id: meta.id, name: meta.name, artist: meta.artist,
    duration: meta.duration_s, bpm: null,
  }, 0);
  if (!res.ok) return toast(res.reason);
  const wf = await api.waveform(meta.id, null);
  timeline.setWaveform(meta.id, wf);
  nav.setTotal(vp, state.totalDuration(mix));
  vp.start = 0; vp.dur = vp.total;
  $("#drop-hint").classList.add("hidden");
  $("#btn-play").disabled = false;
  await player.load(meta.id, null);
  await renderDeckRecommendations(meta.id);
  renderAttributions();
  updateTimes();
  requestDraw();
}

// Append the next track in the chain. Pair analysis is always against the
// CURRENT LAST track — that is the only junction the new track creates.
async function addNextTrack(trackId) {
  const lastIndex = mix.tracks.length - 1;
  const a = mix.tracks[lastIndex];
  const tr = await api.transitions(a.id, trackId);
  currentTransition = tr;
  currentMarkers = tr.markers;
  gridBpm = tr.grid_bpm;

  // Both tracks move to the shared-grid variants (Phase 4: no live stretch).
  const [wfA, wfB] = await Promise.all([
    api.waveform(a.id, gridBpm),
    api.waveform(trackId, gridBpm),
  ]);
  a.bpm = gridBpm;
  a.duration = wfA.duration_s;
  timeline.setWaveform(a.id, wfA);

  const meta = catalog.find((c) => c.id === trackId);
  // The marker offset is relative to the PREVIOUS track's start, which is
  // exactly what a delta is — so the snap needs no conversion.
  const res = state.addTrack(mix, {
    id: meta.id, name: meta.name, artist: meta.artist,
    duration: wfB.duration_s, bpm: gridBpm,
  }, align.snapOffset(currentMarkers));
  if (!res.ok) return toast(res.reason);

  markerOriginIndex = lastIndex;
  timeline.setWaveform(meta.id, wfB);
  timeline.setMarkers(currentMarkers, state.offsets(mix)[lastIndex]);
  nav.setTotal(vp, state.totalDuration(mix));
  // Follow the edit: in a long mix the new junction is usually outside the
  // current view, and a marker lane you cannot see is no use.
  revealTime(state.offsetOf(mix, meta.id));
  await Promise.all([player.load(a.id, gridBpm), player.load(meta.id, gridBpm)]);
  toast(`Snapped to best transition \u2014 ${scorePercent(currentTransition.best.score)}% ` +
        `at ${state.formatTime(state.offsetOf(mix, meta.id))} (${gridBpm} BPM grid)`);
  await renderDeckRecommendations(meta.id);   // suggestions follow the new tail
  renderAttributions();
  updateTimes();
  requestDraw();
}

/** Pan (without changing zoom) so `t` is comfortably inside the viewport. */
function revealTime(t) {
  if (t == null) return;
  const margin = vp.dur * 0.15;
  if (t >= vp.start + margin && t <= vp.start + vp.dur - margin) return;
  vp.start = t - vp.dur / 2;
  nav.clamp(vp);
}

// ------------------------------------------------------------- interaction
const wrap = $(".track-window-wrap");
wrap.addEventListener("dragover", (e) => {
  if (state.canAddTrack(mix)) { e.preventDefault(); wrap.classList.add("drop-ok"); }
});
wrap.addEventListener("dragleave", () => wrap.classList.remove("drop-ok"));
wrap.addEventListener("drop", async (e) => {
  e.preventDefault();
  wrap.classList.remove("drop-ok");
  const id = e.dataTransfer.getData("text/track-id");
  if (!id) return;
  if (!state.canAddTrack(mix)) return toast("Two tracks are already loaded. Remove one to add another.");
  if (mix.tracks.length === 0) await addFirstTrack(id);
  else await addNextTrack(id);
});

// Drag track 2 along the x-axis with magnetic pull; click empty space seeks.
let dragging = null;
const tlCanvas = $("#timeline");
tlCanvas.addEventListener("pointerdown", (e) => {
  const rect = tlCanvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const idx = timeline.trackAtPoint(px, e.clientY - rect.top);
  // Any track after the first can be dragged; the first anchors the mix.
  if (idx !== null && idx > 0) {
    dragging = {
      index: idx,
      id: mix.tracks[idx].id,
      grabDelta: timeline.pxToTimeLocal(px) - state.offsets(mix)[idx],
    };
    tlCanvas.classList.add("dragging");
    tlCanvas.setPointerCapture(e.pointerId);
  } else {
    // Seek (P4-06): cursor moves to the tapped position.
    const t = Math.max(0, Math.min(timeline.pxToTimeLocal(px), state.totalDuration(mix)));
    timeline.setCursor(t);
    player.seek(mix, t);
    updateTimes();
    requestDraw();
  }
});
tlCanvas.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const rect = tlCanvas.getBoundingClientRect();
  const proposed = timeline.pxToTimeLocal(e.clientX - rect.left) - dragging.grabDelta;

  // HARD beat snap: a marker in reach wins, otherwise the nearest beat. There
  // is no off-grid resting place, so a drag can never leave beats misaligned.
  const markers = dragging.index === markerOriginIndex + 1 ? currentMarkers : [];
  const snapped = align.snapOffsetTo(proposed, markers, beatAttractors(), gridBpm);

  // Rigid ripple: setOffset rewrites this track's delta only, so every
  // downstream track keeps its spacing and rides along.
  state.setOffset(mix, dragging.id, snapped);
  nav.setTotal(vp, state.totalDuration(mix));
  updateTimes();
  requestDraw();
});
tlCanvas.addEventListener("pointerup", () => {
  if (!dragging) return;
  dragging = null;
  tlCanvas.classList.remove("dragging");
  if (player.playing) player.seek(mix, timeline.cursor);
});

function beatAttractors() {
  // Absolute times of the mix's reference beat grid. Every track shares the
  // same grid BPM once paired, so track 1's grid is the mix's grid.
  const a = mix.tracks[0];
  const wfA = a && timeline.waveforms.get(a.id);
  if (!wfA?.beat_grid) return [];
  const origin = state.offsets(mix)[0] ?? 0;
  const grid = wfA.beat_grid.map((bt) => bt + origin);
  // Extend the grid across the whole mix: a few-hour set runs far past the
  // first track's own beat list.
  const beat = 60 / (wfA.bpm || gridBpm || 120);
  const total = state.totalDuration(mix);
  for (let t = grid[grid.length - 1] ?? 0; t < total; t += beat) grid.push(t + beat);
  return grid;
}

// ------------------------------------------------------------------ navbar
const nbCanvas = $("#navbar");
let navDrag = null;
function navHit(px) {
  const W = nbCanvas.getBoundingClientRect().width;
  const x0 = (vp.start / vp.total) * W;
  const x1 = ((vp.start + vp.dur) / vp.total) * W;
  if (Math.abs(px - x0) < 7) return "left";
  if (Math.abs(px - x1) < 7) return "right";
  if (px > x0 && px < x1) return "body";
  return "outside";
}
nbCanvas.addEventListener("pointermove", (e) => {
  if (navDrag) return;
  const rect = nbCanvas.getBoundingClientRect();
  const hit = navHit(e.clientX - rect.left);
  nbCanvas.style.cursor = hit === "body" ? "grab" : hit === "outside" ? "pointer" : "ew-resize";
});
nbCanvas.addEventListener("pointerdown", (e) => {
  const rect = nbCanvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const hit = navHit(px);
  const W = rect.width;
  if (hit === "outside") {
    vp.start = (px / W) * vp.total - vp.dur / 2;
    nav.clamp(vp);
  } else {
    navDrag = { mode: hit, lastPx: px, width: W };
    nbCanvas.setPointerCapture(e.pointerId);
  }
  requestDraw();
});
nbCanvas.addEventListener("pointermove", (e) => {
  if (!navDrag) return;
  const rect = nbCanvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const dt = ((px - navDrag.lastPx) / navDrag.width) * vp.total;
  navDrag.lastPx = px;
  if (navDrag.mode === "body") nav.pan(vp, dt);
  else nav.resizeEdge(vp, navDrag.mode, dt);
  requestDraw();
});
nbCanvas.addEventListener("pointerup", () => { navDrag = null; });

function drawNavbar() {
  const ctx = nbCanvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = nbCanvas.getBoundingClientRect();
  nbCanvas.width = rect.width * dpr; nbCanvas.height = rect.height * dpr;
  const W = nbCanvas.width, H = nbCanvas.height;
  ctx.clearRect(0, 0, W, H);
  // Whole-mix silhouette
  const offs = state.offsets(mix);
  mix.tracks.forEach((t, idx) => {
    const wf = timeline.waveforms.get(t.id);
    if (!wf) return;
    ctx.fillStyle = trackColor(idx);
    ctx.globalAlpha = 0.5;
    const n = wf.points.length;
    for (let i = 0; i < n; i += 2) {
      const time = (i / (n - 1)) * wf.duration_s + offs[idx];
      const x = (time / vp.total) * W;
      const h = Math.max(1, wf.points[i] * H * 0.7);
      ctx.fillRect(x, (H - h) / 2, Math.max(1, W / n), h);
    }
    ctx.globalAlpha = 1;
  });
  // Viewport rectangle
  const x0 = (vp.start / vp.total) * W;
  const x1 = ((vp.start + vp.dur) / vp.total) * W;
  ctx.strokeStyle = COLORS.cursor;
  ctx.lineWidth = 1.2 * dpr;
  ctx.strokeRect(x0, 1, x1 - x0, H - 2);
  ctx.fillStyle = "rgba(232,230,223,0.08)";
  ctx.fillRect(x0, 1, x1 - x0, H - 2);
  // Cursor tick
  const cx = (timeline.cursor / vp.total) * W;
  ctx.strokeStyle = COLORS.gold || "#ffc24b";
  ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, H); ctx.stroke();
}

// ---------------------------------------------------------------- playback
$("#btn-play").addEventListener("click", () => {
  if (player.playing) {
    player.stop();
    $("#btn-play").innerHTML = "&#9654;";
  } else {
    player.play(mix, timeline.cursor);
    $("#btn-play").innerHTML = "&#10074;&#10074;";
  }
});

function tick() {
  if (player.playing) {
    // Extend the scheduling horizon as the head advances — an hours-long mix
    // is never fully scheduled at once.
    player.refresh(mix);
    const pos = player.position();
    timeline.setCursor(pos);
    if (pos >= state.totalDuration(mix)) {
      player.stop();
      timeline.setCursor(0);
      $("#btn-play").innerHTML = "&#9654;";
    }
    updateTimes();
    requestDraw();
  }
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------------ redraw
let drawQueued = false;
function requestDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => {
    drawQueued = false;
    timeline.resize();
    timeline.draw();
    drawNavbar();
  });
}
window.addEventListener("resize", requestDraw);

// ------------------------------------------------------------------- title
$("#mix-title").addEventListener("input", (e) => { mix.title = e.target.textContent.trim(); });
$("#mix-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } });

// ----------------------------------------------------------------- credits
$("#btn-credits").addEventListener("click", async () => {
  const list = $("#credits-list");
  list.innerHTML = "";
  for (const c of await api.credits()) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = c.url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = c.name;
    li.appendChild(a);
    li.appendChild(document.createTextNode(` \u2014 ${c.license}${c.note ? ". " + c.note : ""}`));
    list.appendChild(li);
  }
  $("#credits-dialog").showModal();
});

// ------------------------------------------------------------------- boot
// The server binds its port before the catalog exists, so the client waits on
// /api/status and shows progress. Nothing catalog-backed is fetched or drawn
// until warmup reports ready — the user never sees a half-built page.
function paintBootOverlay(status) {
  $("#boot-message").textContent = boot.statusMessage(status);
  $("#boot-detail").textContent = boot.statusDetail(status);
  $("#boot-bar").style.width = `${boot.progressPercent(status)}%`;
  $(".boot-card").classList.toggle("failed", boot.isFailed(status));
}

function hideBootOverlay() {
  const el = $("#boot-overlay");
  el.classList.add("hidden");
  // Remove from the tree once faded so it can never trap focus or clicks.
  setTimeout(() => { el.style.display = "none"; }, 260);
}

async function waitForCatalog() {
  for (let attempt = 0; ; attempt++) {
    let status = null;
    try {
      status = await api.status();
    } catch {
      // Server not accepting connections yet — keep the overlay up and retry.
      paintBootOverlay(null);
    }
    if (status) {
      paintBootOverlay(status);
      if (boot.isReady(status)) return status;
      if (boot.isFailed(status)) throw new Error(boot.statusMessage(status));
    }
    await new Promise((r) => setTimeout(r, boot.pollDelayMs(attempt)));
  }
}

(async function boot_() {
  try {
    await waitForCatalog();
    catalog = await api.tracks();
    await renderDeckZeroState();
    renderAttributions();
    updateTimes();
    requestDraw();
    requestAnimationFrame(tick);
    hideBootOverlay();
  } catch (err) {
    // Leave the overlay up: a failed warmup means there is no catalog to show.
    paintBootOverlay({ phase: "failed", error: String(err.message || err) });
  }
})();
