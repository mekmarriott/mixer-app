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
