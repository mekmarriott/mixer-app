// App wiring: connects state, timeline, navbar, deck, audio, attribution.
import { api } from "./api.js";
import * as state from "./state.js";
import * as align from "./align.js";
import * as nav from "./navbar.js";
import { rankRecommendations, scorePercent, piePath } from "./deck.js";
import { attributionParts, licenseBadges } from "./attribution.js";
import { Timeline, COLORS } from "./timeline.js";
import { Player } from "./audio.js";
import { overlapZone } from "./state.js";

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
    sw.style.background = idx === 0 ? COLORS.track1 : COLORS.track2;
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
async function renderDeckAll() {
  $("#deck-sub").textContent = "All tracks \u2014 pick your opener";
  const rows = catalog.map((t) => deckRow(t, null));
  const deck = $("#deck");
  deck.innerHTML = "";
  rows.forEach((r) => deck.appendChild(r));
}

async function renderDeckRecommendations(forTrackId) {
  const recs = rankRecommendations(await api.recommendations(forTrackId));
  $("#deck-sub").textContent = recs.length
    ? `Suggested next tracks for what\u2019s playing \u2014 ranked by match`
    : "No compatible tracks share a BPM grid with this one.";
  const deck = $("#deck");
  deck.innerHTML = "";
  recs.forEach((rec) => {
    const meta = catalog.find((c) => c.id === rec.track_id);
    deck.appendChild(deckRow(meta, rec));
  });
}

function deckRow(meta, rec) {
  const li = document.createElement("li");
  li.className = "deck-row";
  const draggable = meta.mixable && state.canAddTrack(mix) && !mix.tracks.some((t) => t.id === meta.id);
  li.setAttribute("draggable", draggable ? "true" : "false");
  li.dataset.trackId = meta.id;

  const wf = document.createElement("canvas");
  li.appendChild(wf);
  api.waveform(meta.id, null, 120).then((w) =>
    drawMiniWaveform(wf, w.points, mix.tracks.length === 0 ? COLORS.track1 : COLORS.track2));

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
    duration: meta.duration_s, offset: 0, bpm: null,
  });
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

async function addSecondTrack(trackId) {
  const a = mix.tracks[0];
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
  const res = state.addTrack(mix, {
    id: meta.id, name: meta.name, artist: meta.artist,
    duration: wfB.duration_s, offset: align.snapOffset(currentMarkers), bpm: gridBpm,
  });
  if (!res.ok) return toast(res.reason);
  timeline.setWaveform(meta.id, wfB);
  timeline.setMarkers(currentMarkers);
  nav.setTotal(vp, state.totalDuration(mix));
  await Promise.all([player.load(a.id, gridBpm), player.load(meta.id, gridBpm)]);
  toast(`Snapped to best transition \u2014 ${scorePercent(currentTransition.best.score)}% at ${state.formatTime(mix.tracks[1].offset)} (${gridBpm} BPM grid)`);
  await renderDeckRecommendations(a.id); // rows become disabled at 2 tracks
  renderAttributions();
  updateTimes();
  requestDraw();
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
  else await addSecondTrack(id);
});

// Drag track 2 along the x-axis with magnetic pull; click empty space seeks.
let dragging = null;
const tlCanvas = $("#timeline");
tlCanvas.addEventListener("pointerdown", (e) => {
  const rect = tlCanvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const tr = timeline.trackAtPoint(px, e.clientY - rect.top);
  if (tr && mix.tracks.length === 2 && tr === mix.tracks[1]) {
    dragging = { track: tr, grabDelta: timeline.pxToTimeLocal(px) - tr.offset };
    tlCanvas.classList.add("dragging");
    tlCanvas.setPointerCapture(e.pointerId);
  } else {
    // Seek (P4-06): cursor moves to the tapped position.
    const t = Math.max(0, Math.min(timeline.pxToTimeLocal(px), state.totalDuration(mix)));
    timeline.setCursor(t);
    player.seek(mix, overlapZone(mix), t);
    updateTimes();
    requestDraw();
  }
});
tlCanvas.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const rect = tlCanvas.getBoundingClientRect();
  const proposed = timeline.pxToTimeLocal(e.clientX - rect.left) - dragging.grabDelta;
  const beatTimes = beatAttractors();
  const pulled = align.magneticOffset(proposed, currentMarkers, beatTimes);
  state.setOffset(mix, dragging.track.id, pulled);
  nav.setTotal(vp, state.totalDuration(mix));
  updateTimes();
  requestDraw();
});
tlCanvas.addEventListener("pointerup", () => {
  if (!dragging) return;
  dragging = null;
  tlCanvas.classList.remove("dragging");
  if (player.playing) player.seek(mix, overlapZone(mix), timeline.cursor);
});

function beatAttractors() {
  // Track 2 offsets that put B's first downbeat on A's beat grid.
  const a = mix.tracks[0];
  const wfA = a && timeline.waveforms.get(a.id);
  if (!wfA?.beat_grid) return [];
  return wfA.beat_grid.map((bt) => bt + a.offset);
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
  mix.tracks.forEach((t, idx) => {
    const wf = timeline.waveforms.get(t.id);
    if (!wf) return;
    ctx.fillStyle = idx === 0 ? COLORS.track1 : COLORS.track2;
    ctx.globalAlpha = 0.5;
    const n = wf.points.length;
    for (let i = 0; i < n; i += 2) {
      const time = (i / (n - 1)) * wf.duration_s + t.offset;
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
    player.play(mix, overlapZone(mix), timeline.cursor);
    $("#btn-play").innerHTML = "&#10074;&#10074;";
  }
});

function tick() {
  if (player.playing) {
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
(async function boot() {
  catalog = await api.tracks();
  await renderDeckAll();
  renderAttributions();
  updateTimes();
  requestDraw();
  requestAnimationFrame(tick);
})();
