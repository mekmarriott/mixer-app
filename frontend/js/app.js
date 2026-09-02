// App wiring: connects state, timeline, navbar, deck, audio, attribution.
import { api } from "./api.js";
import * as state from "./state.js";
import * as align from "./align.js";
import * as nav from "./navbar.js";
import { rankRecommendations, scorePercent, piePath } from "./deck.js";
import * as boot from "./boot.js";
import { attributionParts, licenseBadges } from "./attribution.js";
import { Timeline, COLORS, trackColor, RULER_H } from "./timeline.js";
import { Player } from "./audio.js";

const $ = (sel) => document.querySelector(sel);

const mix = state.createMix();
const vp = nav.createViewport(60);
const timeline = new Timeline($("#timeline"), vp);
timeline.setMix(mix);
const player = new Player();

let catalog = [];
// Track metadata by id, from every source the UI has seen.
//
// `catalog` is only the WARMED subset: /api/tracks returns the tracks whose
// analysis was precomputed at startup, but the deck ranks against the whole
// library and inlines the metadata for candidates outside that subset. Looking
// a dragged track up in `catalog` alone therefore missed most deck rows, and
// the drop threw on `meta.id` before anything was added to the mix — the drag
// simply appeared to do nothing. Every lookup goes through `trackMeta` so a
// track the user can SEE is always a track the user can ADD.
const metaSeen = new Map();

function rememberMeta(meta) {
  if (meta && meta.id != null) metaSeen.set(meta.id, meta);
  return meta;
}

function trackMeta(id) {
  return metaSeen.get(id) || catalog.find((c) => c.id === id) || null;
}

/**
 * Preload audio for tracks, reporting what could not be fetched instead of
 * rejecting.
 *
 * A track's geometry — waveform, duration, markers — comes from its analysis,
 * which is stored separately from its audio. So a missing audio blob still
 * leaves a mix that draws and edits correctly; only playback of that one track
 * is unavailable. Rejecting here took the whole caller down with it: on boot
 * that left the overlay up for good, reporting a client-side 404 as though the
 * server's warmup had failed, and no reload could clear it because the same
 * mix was resumed every time.
 */
async function loadAudioFor(tracks) {
  const failed = await Promise.all(tracks.map(async (t) => {
    try {
      await player.load(t.id, t.bpm);
      return null;
    } catch {
      return t.name || t.id;
    }
  }));
  const names = failed.filter(Boolean);
  if (names.length) {
    toast(`Audio unavailable for ${names.join(", ")} \u2014 ${names.length === 1 ? "that track" : "those tracks"} will not play.`);
  }
  return names.length === 0;
}
let currentTransition = null;
let gridBpm = null;
// Markers for EVERY junction, keyed by the index of the junction's left track.
// A chain of N tracks has N-1 junctions and each has its own candidates; one
// shared list meant adding a track erased the previous junction's markers.
const junctions = new Map();
let currentMixId = null;
let mixNodeIds = [];        // chain node ids, parallel to mix.tracks
let saveTimer = null;
let savingCount = 0;

// ------------------------------------------------------------ persistence
//
// Only ordering and one gap per track are stored, so a write is genuinely
// cheap. What is NOT cheap is doing it on every pointermove — that is ~60
// requests a second for a gesture whose outcome is a single number. So a drag
// coalesces: the position is written at most every SAVE_DEBOUNCE_MS while
// moving, and flushed once on release.
const SAVE_DEBOUNCE_MS = 400;

function setSaveIndicator(text, busy = false) {
  const el = $("#mix-saved");
  el.textContent = text;
  el.classList.toggle("saving", busy);
}

/**
 * The grid a track is actually on, or null when it is on none yet.
 *
 * This used to fall back to a literal 120. Grid points are derived from a
 * track's own tempo band, so 120 is a real one for some tracks and impossible
 * for others: a 163 BPM track's grid is [153..178] and never includes it. A
 * track stamped 120 before its BPM was known — which is every first track,
 * since it has no shared grid until a second one joins — then had every audio
 * request for it 404, permanently, because the variant cannot exist.
 *
 * The fallback is therefore taken from the track's OWN rendered grid, which is
 * the set of tempos it actually has variants at, so whatever is chosen can be
 * served. mix_tracks.grid_bpm is INTEGER NOT NULL, so there is no "no grid"
 * to store here — but there is always a real grid point to store instead.
 */
function gridBpmFor(track) {
  if (track && track.bpm) return track.bpm;
  if (gridBpm) return gridBpm;
  const id = track && track.id;
  const grids = renderedGridsFor(id);
  if (grids.length) return grids[0];
  // No rendered variants at all: the native tempo is still a truthful answer,
  // and the API falls back to the master when no variant matches.
  const meta = trackMeta(id);
  return (meta && meta.bpm) || null;
}

/** Tempos this track actually has variants rendered at. */
function renderedGridsFor(id) {
  const meta = trackMeta(id);
  if (meta && meta.grid_bpms && meta.grid_bpms.length) return meta.grid_bpms;
  // A deck row's inlined metadata may omit the grid list; the catalog row for
  // the same track carries it.
  const row = catalog.find((c) => c.id === id);
  return (row && row.grid_bpms) || [];
}

function beatsFor(track) {
  const bpm = gridBpmFor(track);
  // The server stores beats as 0 when there is no grid to measure them against
  // (mixes.seconds_to_beats); inventing a tempo here would disagree with it.
  if (!bpm) return 0;
  return align.beatsBetween(track.delta ?? 0, bpm);
}

/** Persist one track's position — one row, one column. */
async function saveTrackPosition(index) {
  const node = mixNodeIds[index];
  const track = mix.tracks[index];
  if (!currentMixId || !node || !track) return;
  savingCount++;
  setSaveIndicator("Saving\u2026", true);
  try {
    await api.moveMixTrack(currentMixId, node, beatsFor(track));
    setSaveIndicator("Saved");
  } catch (err) {
    setSaveIndicator("Not saved");
    toast(`Could not save position: ${err.message}`);
  } finally {
    if (--savingCount === 0) setTimeout(() => setSaveIndicator(""), 1800);
  }
}

/** Persist the whole chain — used for structural edits (append/insert/remove). */
async function saveChain() {
  if (!currentMixId) return;
  savingCount++;
  setSaveIndicator("Saving\u2026", true);
  try {
    const payload = mix.tracks.map((t, i) => ({
      node_id: mixNodeIds[i] || null,
      track_id: t.id,
      delta_beats: beatsFor(t),
      grid_bpm: Math.round(gridBpmFor(t)),
    }));
    const saved = await api.putMixTracks(currentMixId, payload);
    mixNodeIds = saved.tracks.map((t) => t.node_id);
    setSaveIndicator("Saved");
    await refreshMixList();
  } catch (err) {
    setSaveIndicator("Not saved");
    toast(`Could not save mix: ${err.message}`);
  } finally {
    if (--savingCount === 0) setTimeout(() => setSaveIndicator(""), 1800);
  }
}

let pendingSaveIndex = null;
function scheduleMixSave() {
  if (!currentMixId || !dragging) return;
  pendingSaveIndex = dragging.index;
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    if (pendingSaveIndex !== null) saveTrackPosition(pendingSaveIndex);
  }, SAVE_DEBOUNCE_MS);
}

function flushMixSave() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (pendingSaveIndex !== null) {
    saveTrackPosition(pendingSaveIndex);
    pendingSaveIndex = null;
  }
}

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
    const meta = trackMeta(t.id);
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

//: One page of suggestions. The whole ranked list is rarely looked at, and
//: every row returned costs a payload to build, so the deck asks for a page
//: and extends it only as far as the user actually scrolls.
const DECK_PAGE = 10;

// Which track the visible deck belongs to, how far into its ranking we have
// read, and whether the ranking ran out. Tracked so a scroll cannot append a
// page belonging to a track the deck has since moved off.
let deckPaging = { forTrackId: null, offset: 0, exhausted: true, loading: false };

async function renderDeckRecommendations(forTrackId) {
  // Suggestions are an aid, never a precondition. Ranking a track against the
  // whole library is the most expensive read the API serves, and when it times
  // out the rejection used to travel all the way up into boot's catch, which
  // left the warmup overlay up for good. An empty deck is a far smaller loss
  // than an app that will not open.
  deckPaging = { forTrackId, offset: 0, exhausted: false, loading: false };
  const deck = $("#deck");
  deck.innerHTML = "";
  const list = document.createElement("ol");
  list.className = "deck-list";
  deck.appendChild(list);

  const first = await loadDeckPage(forTrackId, list);
  if (first === null) {
    $("#deck-sub").textContent = "Suggestions are unavailable for this track.";
    deck.innerHTML = "";
    return;
  }
  $("#deck-sub").textContent = first
    ? `Suggested next tracks for what\u2019s playing \u2014 ranked by match`
    : "No compatible tracks share a BPM grid with this one.";
}

/**
 * Append the next page of suggestions.
 *
 * Returns how many rows were added, or null if the request failed. A short
 * page means the ranking is spent, which is what stops the scroll handler from
 * asking again forever.
 */
async function loadDeckPage(forTrackId, list) {
  if (deckPaging.loading || deckPaging.exhausted) return 0;
  deckPaging.loading = true;
  let recs;
  try {
    recs = rankRecommendations(await api.recommendations(forTrackId, {
      limit: DECK_PAGE, offset: deckPaging.offset,
    }));
  } catch {
    deckPaging.loading = false;
    deckPaging.exhausted = true;
    return null;
  }
  // The deck may have moved to another track while this was in flight; its
  // rows belong to a ranking that is no longer on screen.
  if (deckPaging.forTrackId !== forTrackId) {
    deckPaging.loading = false;
    return 0;
  }
  recs.forEach((rec) => {
    // The API inlines the candidate's metadata and waveform, so a deck row
    // needs no follow-up request either.
    const meta = rememberMeta(rec.track) || trackMeta(rec.track_id);
    if (meta) list.appendChild(deckRow(meta, rec));
  });
  deckPaging.offset += recs.length;
  deckPaging.exhausted = recs.length < DECK_PAGE;
  deckPaging.loading = false;
  return recs.length;
}

// Extend the deck as it is scrolled into view. Bound to the window because the
// deck grows the page rather than scrolling inside its own box.
window.addEventListener("scroll", () => {
  if (deckPaging.exhausted || deckPaging.loading || !deckPaging.forTrackId) return;
  const nearBottom =
    window.innerHeight + window.scrollY >= document.body.offsetHeight - 400;
  if (!nearBottom) return;
  const list = $("#deck").querySelector(".deck-list");
  if (list) loadDeckPage(deckPaging.forTrackId, list);
}, { passive: true });

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
  const meta = trackMeta(trackId);
  if (!meta) return toast("That track is no longer available.");
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
  await loadAudioFor([mix.tracks[mix.tracks.length - 1]]);
  await renderDeckRecommendations(meta.id);
  renderAttributions();
  updateTimes();
  requestDraw();
  await saveChain();
}

// Append the next track in the chain. Pair analysis is always against the
// CURRENT LAST track — that is the only junction the new track creates.
async function addNextTrack(trackId) {
  const meta = trackMeta(trackId);
  if (!meta) return toast("That track is no longer available.");
  const lastIndex = mix.tracks.length - 1;
  const a = mix.tracks[lastIndex];
  const tr = await api.transitions(a.id, trackId);
  currentTransition = tr;
  gridBpm = tr.grid_bpm;
  const currentMarkers = tr.markers;

  // Both tracks move to the shared-grid variants (Phase 4: no live stretch).
  const [wfA, wfB] = await Promise.all([
    api.waveform(a.id, gridBpm),
    api.waveform(trackId, gridBpm),
  ]);
  a.bpm = gridBpm;
  a.duration = wfA.duration_s;
  timeline.setWaveform(a.id, wfA);

  // The marker offset is relative to the PREVIOUS track's start, which is
  // exactly what a delta is — so the snap needs no conversion.
  // Add provisionally, then place. The floor depends on the chain geometry —
  // specifically on the track two back — so it can only be computed once this
  // track is in the chain.
  const res = state.addTrack(mix, {
    id: meta.id, name: meta.name, artist: meta.artist,
    duration: wfB.duration_s, bpm: gridBpm,
  }, 0);
  if (!res.ok) return toast(res.reason);

  // The best marker is not always a legal position: it can place the incoming
  // track early enough to reach back into its second-nearest predecessor, and
  // the server refuses that (3 tracks on the grid at once). The drag path has
  // always clamped; the drop path did not, so a drop could propose a placement
  // the API had to reject — the save failed with a 409 and the mix silently
  // did not persist.
  const placedIndex = mix.tracks.length - 1;
  const prevStart = state.offsets(mix)[placedIndex - 1] ?? 0;
  const minDelta = Math.max(0, state.minOffsetFor(mix, placedIndex) - prevStart);
  state.setDelta(mix, meta.id, align.placementOffset(currentMarkers, {
    prevDuration: a.duration, minDelta,
  }));

  junctions.set(lastIndex, { markers: currentMarkers, gridBpm });
  timeline.setWaveform(meta.id, wfB);
  syncMarkerGroups();
  nav.setTotal(vp, state.totalDuration(mix));
  // Follow the edit: in a long mix the new junction is usually outside the
  // current view, and a marker lane you cannot see is no use.
  revealTime(state.offsetOf(mix, meta.id));
  await loadAudioFor([a, mix.tracks[mix.tracks.length - 1]]);
  toast(`Snapped to best transition \u2014 ${scorePercent(currentTransition.best.score)}% ` +
        `at ${state.formatTime(state.offsetOf(mix, meta.id))} (${gridBpm} BPM grid)`);
  await renderDeckRecommendations(meta.id);   // suggestions follow the new tail
  renderAttributions();
  updateTimes();
  requestDraw();
  await saveChain();
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

// Two independent drags share this canvas:
//   * the PLAYHEAD, grabbed by its handle — scrubs the play position;
//   * a TRACK, grabbed by its waveform — moves it in time (rigid ripple).
// The playhead is hit-tested first and is NOT beat-snapped: it is a viewing
// position, not a musical one, and quantising it would make fine seeking
// impossible. Clicking bare canvas still seeks, as before.
let dragging = null;
let scrubbing = null;
let selectedIndex = null;   // track selected for deletion
const tlCanvas = $("#timeline");
function seekTo(t) {
  const clamped = Math.max(0, Math.min(t, state.totalDuration(mix)));
  timeline.setCursor(clamped);
  player.seek(mix, clamped);
  updateTimes();
  requestDraw();
  return clamped;
}

tlCanvas.addEventListener("pointerdown", (e) => {
  const rect = tlCanvas.getBoundingClientRect();
  const px = e.clientX - rect.left;

  const localY = e.clientY - rect.top;
  // The playhead wins over whatever is beneath it — once a mix has tracks, it
  // is always over one, and it has to stay reachable. The top strip is also a
  // scrub ruler, so the playhead can be moved even when it is off-screen.
  if (timeline.cursorAtPoint(px) || localY < RULER_H) {
    // Stop the transport for the duration of the scrub so audio does not run
    // away from the handle, and resume from wherever it is released.
    scrubbing = { wasPlaying: player.playing };
    if (player.playing) {
      player.stop();
      $("#btn-play").innerHTML = "&#9654;";
    }
    tlCanvas.classList.add("scrubbing");
    tlCanvas.setPointerCapture(e.pointerId);
    seekTo(timeline.pxToTimeLocal(px));
    return;
  }

  // The delete badge on the selected track, before anything beneath it.
  const badge = timeline.deleteBadgeAtPoint(px, localY);
  if (badge !== null) {
    deleteTrackAt(badge);
    return;
  }

  const idx = timeline.trackAtPoint(px, localY);
  // Clicking a track selects it — that is what Delete acts on.
  if (idx !== timeline.selected) {
    selectedIndex = idx;
    timeline.selected = idx;
    requestDraw();
  }
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
    seekTo(timeline.pxToTimeLocal(px));
  }
});
tlCanvas.addEventListener("pointermove", (e) => {
  const rect = tlCanvas.getBoundingClientRect();
  const localX = e.clientX - rect.left;

  if (scrubbing) {
    seekTo(timeline.pxToTimeLocal(localX));
    return;
  }

  // Hover affordance: the handle lights up and the cursor changes, so it is
  // discoverable that the playhead can be grabbed at all.
  if (!dragging) {
    const hot = timeline.cursorAtPoint(localX);
    if (hot !== timeline.cursorHot) {
      timeline.cursorHot = hot;
      tlCanvas.style.cursor = hot ? "ew-resize" : "";
      requestDraw();
    }
    return;
  }
  if (!dragging) return;
  const proposed = timeline.pxToTimeLocal(e.clientX - rect.left) - dragging.grabDelta;

  // HARD beat snap: a marker in reach wins, otherwise the nearest beat. There
  // is no off-grid resting place, so a drag can never leave beats misaligned.
  // Markers come from THIS track's own junction, so every track in the chain
  // snaps to its own candidates — not just the most recently added one.
  const snapped = align.snapOffsetTo(
    proposed, markersForTrack(dragging.index), beatAttractors(), gridBpm);

  // At most two tracks may overlap: clamp before storing, so the drag simply
  // stops rather than producing a state the API would reject.
  const legal = state.clampOffset(mix, dragging.index, snapped);
  // Re-snap after clamping — the clamp floor is not necessarily on a beat.
  const onGrid = legal > snapped
    ? align.snapOffsetTo(legal + 1e-6, [], beatAttractors(), gridBpm)
    : snapped;

  // Rigid ripple: setOffset rewrites this track's delta only, so every
  // downstream track keeps its spacing and rides along.
  state.setOffset(mix, dragging.id, Math.max(legal, onGrid));
  scheduleMixSave();
  nav.setTotal(vp, state.totalDuration(mix));
  updateTimes();
  requestDraw();
});
tlCanvas.addEventListener("pointerup", () => {
  if (scrubbing) {
    // Resume only if the transport was running when the scrub began — a
    // paused scrub stays paused at the new position.
    if (scrubbing.wasPlaying) {
      player.play(mix, timeline.cursor);
      $("#btn-play").innerHTML = "&#10074;&#10074;";
    }
    scrubbing = null;
    tlCanvas.classList.remove("scrubbing");
    return;
  }
  if (!dragging) return;
  flushMixSave();
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
  // Without a tempo the grid cannot be extended; the track's own beat list is
  // still correct as far as it goes, which is better than ruling lines at a
  // tempo the track is not playing at.
  const bpm = wfA.bpm || gridBpm;
  if (!bpm) return grid;
  const beat = 60 / bpm;
  const total = state.totalDuration(mix);
  for (let t = grid[grid.length - 1] ?? 0; t < total; t += beat) grid.push(t + beat);
  return grid;
}

// Delete/Backspace removes the selected track. Ignored while typing in the
// title, which is a contenteditable and owns those keys.
window.addEventListener("keydown", (e) => {
  if (e.key !== "Delete" && e.key !== "Backspace") return;
  const el = document.activeElement;
  if (el && (el.isContentEditable || el.tagName === "INPUT" || el.tagName === "SELECT")) return;
  if (selectedIndex === null || !mix.tracks[selectedIndex]) return;
  e.preventDefault();
  const idx = selectedIndex;
  selectedIndex = null;
  timeline.selected = null;
  deleteTrackAt(idx);
});

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

// -------------------------------------------------------------- mix picker
async function refreshMixList(selectId = currentMixId) {
  const sel = $("#mix-select");
  const mixes = await api.mixes();
  sel.innerHTML = "";

  const neu = document.createElement("option");
  neu.value = "__new__";
  neu.textContent = "+ New mix";
  sel.appendChild(neu);

  // Most recently edited first — the server orders by updated_at. The native
  // listbox scrolls once there are more than fit, so a long history needs no
  // extra handling here.
  for (const m of mixes) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = `${m.name} \u00b7 ${m.track_count} track${m.track_count === 1 ? "" : "s"}`;
    sel.appendChild(o);
  }
  sel.value = selectId && mixes.some((m) => m.id === selectId) ? selectId : "__new__";
}

/** Reset to the zero state: no tracks, browse-by-genre deck. */
async function showZeroState() {
  mix.tracks.length = 0;
  mixNodeIds = [];
  junctions.clear();
  selectedIndex = null;
  timeline.selected = null;
  currentTransition = null;
  gridBpm = null;
  timeline.setMarkerGroups([]);
  timeline.waveforms.clear();
  player.stop();
  $("#btn-play").disabled = true;
  $("#btn-play").innerHTML = "&#9654;";
  $("#drop-hint").classList.remove("hidden");
  timeline.setCursor(0);
  nav.setTotal(vp, 0);
  vp.start = 0; vp.dur = vp.total;
  await renderDeckZeroState();
  renderAttributions();
  updateTimes();
  requestDraw();
}

/**
 * Load a saved mix. A mix with tracks resumes where it left off — the deck
 * shows what to play NEXT, ranked against the last track, rather than dropping
 * back to the browse view. An empty mix is the zero state.
 */
async function loadMix(id) {
  const data = await api.mix(id);
  currentMixId = data.id;
  mix.title = data.name;
  $("#mix-title").textContent = data.name;

  if (!data.tracks.length) {
    await showZeroState();
    await refreshMixList(id);
    return;
  }

  mix.tracks.length = 0;
  mixNodeIds = [];
  junctions.clear();
  timeline.waveforms.clear();
  gridBpm = data.tracks[data.tracks.length - 1].grid_bpm || null;

  for (const entry of data.tracks) {
    const meta = trackMeta(entry.track_id) || await api.track(entry.track_id).catch(() => null);
    if (!meta) continue;
    rememberMeta(meta);
    const wf = await api.waveform(entry.track_id, entry.grid_bpm);
    timeline.setWaveform(entry.track_id, wf);
    state.addTrack(mix, {
      id: meta.id, name: meta.name, artist: meta.artist,
      duration: wf.duration_s, bpm: entry.grid_bpm,
    }, entry.delta_s);
    mixNodeIds.push(entry.node_id);
  }

  await loadAudioFor(mix.tracks);

  // Markers for EVERY junction, not just the last: a resumed mix must show the
  // same transition points it had when it was built. The curve is prefix-sum
  // backed (~1ms server-side) so fetching them in parallel is cheap.
  const last = mix.tracks[mix.tracks.length - 1];
  await Promise.all(mix.tracks.slice(0, -1).map(async (trk, i) => {
    try {
      const tr = await api.transitions(trk.id, mix.tracks[i + 1].id);
      junctions.set(i, { markers: tr.markers, gridBpm: tr.grid_bpm });
      if (i === mix.tracks.length - 2) currentTransition = tr;
    } catch { /* no shared grid for this pair: no markers to show */ }
  }));
  syncMarkerGroups();

  $("#drop-hint").classList.add("hidden");
  $("#btn-play").disabled = false;
  nav.setTotal(vp, state.totalDuration(mix));
  vp.start = 0; vp.dur = vp.total;
  // Resume the session: suggest what comes next, not what to start with.
  await renderDeckRecommendations(last.id);
  renderAttributions();
  updateTimes();
  requestDraw();
  await refreshMixList(id);
}

$("#mix-select").addEventListener("change", async (e) => {
  const value = e.target.value;
  try {
    if (value === "__new__") {
      const created = await api.createMix("Untitled Mix");
      currentMixId = created.id;
      mix.title = created.name;
      $("#mix-title").textContent = created.name;
      await showZeroState();
      await refreshMixList(created.id);
    } else {
      await loadMix(value);
    }
  } catch (err) {
    toast(`Could not open mix: ${err.message}`);
  }
});

// ------------------------------------------------------------------- title
$("#mix-title").addEventListener("input", (e) => { mix.title = e.target.textContent.trim(); });
$("#mix-title").addEventListener("blur", async () => {
  const name = mix.title || "Untitled Mix";
  if (!currentMixId) return;
  try {
    await api.renameMix(currentMixId, name);
    await refreshMixList(currentMixId);
  } catch (err) {
    toast(`Could not rename mix: ${err.message}`);
  }
});
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
/** Push every junction's markers to the renderer, with its own origin. */
function syncMarkerGroups() {
  const offs = state.offsets(mix);
  const groups = [];
  for (const [leftIndex, data] of junctions) {
    if (leftIndex >= mix.tracks.length - 1) continue;   // junction no longer exists
    groups.push({ origin: offs[leftIndex] ?? 0, markers: data.markers });
  }
  timeline.setMarkerGroups(groups);
}

/**
 * Refetch every junction's transition curve and re-grid the tracks.
 *
 * Called after a structural edit (delete, and on load), because removing a
 * track creates a junction between two tracks that were never neighbours: the
 * curve, and possibly the shared grid BPM, are both different.
 */
async function refreshJunctions() {
  junctions.clear();
  if (mix.tracks.length < 2) { syncMarkerGroups(); return; }

  await Promise.all(mix.tracks.slice(0, -1).map(async (trk, i) => {
    const next = mix.tracks[i + 1];
    try {
      const tr = await api.transitions(trk.id, next.id);
      junctions.set(i, { markers: tr.markers, gridBpm: tr.grid_bpm });
      if (i === mix.tracks.length - 2) { currentTransition = tr; gridBpm = tr.grid_bpm; }
    } catch {
      // No shared grid for this pair: no markers, and nothing to snap to.
    }
  }));

  // A changed grid BPM changes a track's rendered length, so the waveform has
  // to follow or the timeline would draw the wrong duration.
  await Promise.all(mix.tracks.map(async (trk, i) => {
    const grid = junctions.get(i)?.gridBpm ?? junctions.get(i - 1)?.gridBpm ?? null;
    if (grid == null || trk.bpm === grid) return;
    const wf = await api.waveform(trk.id, grid);
    trk.bpm = grid;
    trk.duration = wf.duration_s;
    timeline.setWaveform(trk.id, wf);
    await player.load(trk.id, grid);
  }));
  syncMarkerGroups();
}

/**
 * Remove a track and heal the chain.
 *
 * The successor drops onto the BEST transition point with its new predecessor
 * — the two were never neighbours, so its old gap is meaningless. Everything
 * after the successor keeps its own delta, so those transitions survive
 * exactly as they were (rigid ripple).
 *
 * Deleting the head is the one case with no re-snap to make: the successor
 * becomes the start of the mix.
 */
async function deleteTrackAt(index) {
  const track = mix.tracks[index];
  if (!track) return;

  state.removeTrack(mix, track.id);
  mixNodeIds.splice(index, 1);
  if (selectedIndex !== null && selectedIndex >= mix.tracks.length) selectedIndex = null;

  await refreshJunctions();

  if (index === 0) {
    // New head: a leading delta is an absolute start.
    if (mix.tracks.length) state.setDelta(mix, mix.tracks[0].id, 0);
  } else if (index < mix.tracks.length) {
    // The successor now follows a track it never followed before.
    const markers = junctions.get(index - 1)?.markers ?? [];
    const snapped = markers.length
      ? align.snapOffset(markers)
      : state.minOffsetFor(mix, index) - state.offsets(mix)[index - 1];
    state.setDelta(mix, mix.tracks[index].id, Math.max(0, snapped));
  }

  syncMarkerGroups();
  nav.setTotal(vp, state.totalDuration(mix));
  if (!mix.tracks.length) {
    await showZeroState();
  } else {
    await renderDeckRecommendations(mix.tracks[mix.tracks.length - 1].id);
    renderAttributions();
    updateTimes();
    requestDraw();
  }
  await saveChain();
  toast(`Removed \u201c${track.name}\u201d`);
}

/** Markers that govern where track `index` may start (its left junction). */
function markersForTrack(index) {
  return junctions.get(index - 1)?.markers ?? [];
}

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
    catalog.forEach(rememberMeta);

    // Resume the most recently edited mix; if there are none, start one so
    // that everything the user does from here is already being saved.
    // Reopening the last mix is a convenience, not a precondition. Boot
    // resumes the SAME mix on every load, so a mix that cannot be restored —
    // a track whose audio is gone, a suggestion query that times out — would
    // otherwise brick the app permanently, with no reload able to clear it.
    // Fall back to a new mix instead, which is also what a first-time visitor
    // gets.
    const existing = await api.mixes();
    let restored = false;
    if (existing.length) {
      try {
        await loadMix(existing[0].id);
        restored = true;
      } catch {
        toast("Could not reopen the last mix \u2014 started a new one instead.");
      }
    }
    if (!restored) {
      const created = await api.createMix("Untitled Mix");
      currentMixId = created.id;
      await showZeroState();
      await refreshMixList(created.id);
    }

    requestAnimationFrame(tick);
    hideBootOverlay();
  } catch (err) {
    // Leave the overlay up: a failed warmup means there is no catalog to show.
    paintBootOverlay({ phase: "failed", error: String(err.message || err) });
  }
})();
