// Crossfade gain curves — pure, no DOM/WebAudio (tests P4-02, P4-25).
//
// Single source of truth: audio.js schedules THESE values onto GainNodes and
// timeline.js scales waveform amplitude with THE SAME function, so the rendered
// crossfade is truthful to what is audible (ui-requirements §5).
//
// With a chain of tracks rather than a pair, an interior track sits inside TWO
// zones at once: it fades IN across the overlap with its predecessor and OUT
// across the overlap with its successor. Its gain is the product of the two.
// Only neighbours ever overlap, so at any instant at most two tracks sound.

// How long a fade runs when nothing has said otherwise. Bars, not seconds, so
// it stays the same musical length at any tempo (4/4: bar_s = 4 * 60 / bpm).
export const DEFAULT_FADE_BARS = 8;

export function defaultFadeS(bpm) {
  if (!bpm || bpm <= 0) return null;
  return DEFAULT_FADE_BARS * 4 * (60 / bpm);
}

/**
 * The part of an overlap the fade actually occupies.
 *
 * The fade used to be the whole overlap, but an overlap is placed by the
 * marker search — it is wherever the two tracks line up best, and its length
 * is a consequence of that placement, not a musical decision. So a good
 * transition point could hand the mix a fade minutes long. The fade now starts
 * where the incoming track enters and runs its own length, clamped to the
 * overlap; `overlapEnd` keeps the full geometry for anything that needs it.
 */
export function fadeZone(overlap, fadeS) {
  if (!overlap) return null;
  const span = overlap.end - overlap.start;
  if (!(span > 0)) return overlap;
  const len = fadeS == null ? span : Math.min(Math.max(0, fadeS), span);
  return { ...overlap, end: overlap.start + len, overlapEnd: overlap.end };
}

// Equal-power crossfade: a = cos(x*pi/2), b = sin(x*pi/2). Constant perceived
// loudness across the overlap; both curves are monotonic.
export function gainAt(t, fadeStart, fadeEnd, role) {
  if (fadeEnd <= fadeStart) return 1;
  const x = Math.min(1, Math.max(0, (t - fadeStart) / (fadeEnd - fadeStart)));
  return role === "out" ? Math.cos((x * Math.PI) / 2) : Math.sin((x * Math.PI) / 2);
}

/** Gain contributed by a single zone; 1 outside it. */
function zoneGain(t, zone, role) {
  if (!zone) return 1;
  if (role === "out") {
    if (t < zone.start) return 1;
    return gainAt(t, zone.start, zone.end, "out");
  }
  if (t > zone.end) return 1;
  return gainAt(t, zone.start, zone.end, "in");
}

/**
 * Gain applied to one track at mix-time `t`.
 *
 * `incoming` is the zone shared with the previous track (this track fades in
 * across it); `outgoing` is the zone shared with the next (this track fades out
 * across it). Either may be null at the ends of the mix.
 */
export function trackGainAt(t, { incoming = null, outgoing = null } = {}) {
  return zoneGain(t, incoming, "in") * zoneGain(t, outgoing, "out");
}

/**
 * Sampled curve for GainNode.setValueCurveAtTime, covering `from`..`to` in
 * mix time. Sampling the composed gain (rather than scheduling each zone
 * separately) keeps a track that fades in and out inside one buffer correct.
 */
export function gainCurve(from, to, overlaps, points = 256) {
  const out = new Array(points);
  for (let i = 0; i < points; i++) {
    const t = from + ((to - from) * i) / (points - 1);
    out[i] = trackGainAt(t, overlaps);
  }
  return out;
}
