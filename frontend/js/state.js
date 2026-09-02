// Mix state — pure logic, no DOM.
//
// A mix is an ORDERED CHAIN of tracks. Each track stores `delta`: how long
// after the PREVIOUS track's start this one begins (the first track's delta is
// its absolute start, normally 0). Absolute positions are derived by walking
// the chain — they are never stored.
//
// Why relative and not absolute offsets
// -------------------------------------
// This is what makes RIGID RIPPLE free. Nudging one track rewrites exactly one
// delta; every downstream track keeps its own delta and therefore moves with
// it, preserving every transition after the edit. With absolute offsets the
// same nudge would have to rewrite every downstream position — O(n) work per
// drag frame on a 100-track mix, and any missed row silently breaks a
// transition.
//
// It is also the in-memory mirror of the persisted schema (mix_tracks.
// delta_beats), so saving a mix is a direct write of what is already here.
//
// The two-track cap came from ui-requirements.md §Overlay ("two overlapping
// tracks is the max for v1"). That constraint was lifted deliberately to
// support hours-long mixes; see docs/automation-test-manifest.md for what it
// means for testing-document P4-18.

import { fadeZone, defaultFadeS } from "./crossfade.js";

export const MAX_TRACKS = 100;

export function createMix() {
  return { title: "Untitled Mix", tracks: [] };
}

export function canAddTrack(mix) {
  return mix.tracks.length < MAX_TRACKS;
}

// ---------------------------------------------------------------- geometry

/** Absolute start time of every track, derived by running the chain. */
export function offsets(mix) {
  let t = 0;
  return mix.tracks.map((tr) => (t += tr.delta ?? 0));
}

export function offsetOf(mix, id) {
  const i = mix.tracks.findIndex((t) => t.id === id);
  return i < 0 ? null : offsets(mix)[i];
}

export function indexOf(mix, id) {
  return mix.tracks.findIndex((t) => t.id === id);
}

export function totalDuration(mix) {
  const offs = offsets(mix);
  return mix.tracks.reduce((m, t, i) => Math.max(m, offs[i] + t.duration), 0);
}

// ------------------------------------------------------------------- edits

/**
 * Append to the end of the chain. `delta` is measured from the previous
 * track's start; for the first track it is the absolute start.
 */
export function addTrack(mix, track, delta = 0) {
  return insertTrack(mix, mix.tracks.length, track, delta);
}

/**
 * Insert at `index`. RIGID RIPPLE: the inserted track takes `delta` from its
 * new predecessor, and everything after it keeps its own delta — so the whole
 * tail shifts later by exactly `delta`, carrying its transitions intact.
 */
export function insertTrack(mix, index, track, delta = 0) {
  if (!canAddTrack(mix)) {
    return { ok: false,
             reason: `This mix is full (${MAX_TRACKS} tracks). Remove one to add another.` };
  }
  const at = Math.max(0, Math.min(index, mix.tracks.length));
  const t = { ...track, delta: Math.max(0, delta) };
  mix.tracks.splice(at, 0, t);
  return { ok: true, track: t, index: at };
}

/**
 * Remove a track. RIGID RIPPLE: the successor keeps its own delta, so the tail
 * closes up by exactly the removed track's delta and every surviving
 * transition keeps its shape.
 */
export function removeTrack(mix, id) {
  const i = indexOf(mix, id);
  if (i < 0) return false;
  mix.tracks.splice(i, 1);
  // The new first track absorbs nothing: a leading delta is an absolute start,
  // so dropping the head would otherwise shift the whole mix backwards.
  if (i === 0 && mix.tracks.length) mix.tracks[0].delta = 0;
  return true;
}

/**
 * Set one track's gap from its predecessor. This is the ripple edit: only this
 * delta changes, and the entire downstream tail rides along unchanged.
 */
export function setDelta(mix, id, delta) {
  const t = mix.tracks.find((x) => x.id === id);
  if (!t) return false;
  t.delta = Math.max(0, delta);
  return true;
}

/** Move a track to an absolute start time, expressed as a delta edit. */
export function setOffset(mix, id, offset) {
  const i = indexOf(mix, id);
  if (i < 0) return false;
  const prevStart = i === 0 ? 0 : offsets(mix)[i - 1];
  return setDelta(mix, id, Math.max(0, offset) - prevStart);
}

// ---------------------------------------------------------------- overlaps

/**
 * Overlap zone between consecutive tracks `i` and `i+1`, or null if they do
 * not overlap. Only neighbours can overlap: a mix is a chain of pairwise
 * transitions, not an N-way layering.
 */
export function overlapAt(mix, i) {
  const a = mix.tracks[i], b = mix.tracks[i + 1];
  if (!a || !b) return null;
  const offs = offsets(mix);
  const start = Math.max(offs[i], offs[i + 1]);
  const end = Math.min(offs[i] + a.duration, offs[i + 1] + b.duration);
  if (!(end > start)) return null;
  // Where the tracks overlap is geometry; how long the fade runs is a musical
  // choice, carried by the INCOMING track — it is the one being brought in,
  // and the server graded the length from that junction's own blend.
  const fadeS = b.fadeS ?? defaultFadeS(b.bpm || a.bpm);
  return fadeZone({ start, end, from: i, to: i + 1 }, fadeS);
}

/** Every transition zone in the mix, in order. */
export function overlapZones(mix) {
  const out = [];
  for (let i = 0; i < mix.tracks.length - 1; i++) {
    const z = overlapAt(mix, i);
    if (z) out.push(z);
  }
  return out;
}

/**
 * The two zones bearing on one track: the one it fades IN across (shared with
 * its predecessor) and the one it fades OUT across (shared with its
 * successor). Either may be null at the ends of the mix.
 */
export function overlapsFor(mix, i) {
  return { incoming: overlapAt(mix, i - 1), outgoing: overlapAt(mix, i) };
}

// At most two tracks may sound at once, so a track must never reach its
// second-nearest neighbour. Mirrors backend/mixes.py:check_overlaps — the API
// enforces the same rule, so a drag is clamped to exactly what will be
// accepted rather than being rejected after the fact.
export const MAX_SIMULTANEOUS = 2;

/**
 * Smallest legal absolute start for the track at `index`.
 *
 * Three constraints, all consequences of the same rule:
 *   - it may not start before its predecessor (that would reorder the mix);
 *   - it may not reach back into its second-nearest predecessor;
 *   - dragging it earlier drags its successor with it (rigid ripple), so the
 *     successor must not reach back into *its* second-nearest predecessor.
 */
export function minOffsetFor(mix, index) {
  if (index <= 0) return 0;
  const offs = offsets(mix);
  const tracks = mix.tracks;
  let floor = offs[index - 1];

  const back = index - MAX_SIMULTANEOUS;
  if (back >= 0) floor = Math.max(floor, offs[back] + tracks[back].duration);

  const next = tracks[index + 1];
  if (next) {
    const prev = tracks[index - 1];
    floor = Math.max(floor, offs[index - 1] + prev.duration - (next.delta ?? 0));
  }
  return Math.max(0, floor);
}

/** Clamp a proposed absolute start to the legal range. */
export function clampOffset(mix, index, proposed) {
  return Math.max(minOffsetFor(mix, index), proposed);
}

export function formatTime(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  // Hours appear only once the mix is that long — a few-hour set reads
  // naturally without padding every short mix to 0:00:00.
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
}
