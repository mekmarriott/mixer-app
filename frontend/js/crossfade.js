// Crossfade gain curves — pure, no DOM/WebAudio (tests P4-02, P4-25).
//
// Single source of truth: audio.js schedules THESE arrays onto GainNodes and
// timeline.js scales waveform amplitude with THE SAME function, so the
// rendered crossfade is truthful to what is audible (ui-requirements §5).

// Equal-power crossfade: a = cos(x*pi/2), b = sin(x*pi/2). Constant perceived
// loudness across the overlap; both curves are monotonic.
export function gainAt(t, fadeStart, fadeEnd, role) {
  if (fadeEnd <= fadeStart) return role === "out" ? 1 : 1;
  const x = Math.min(1, Math.max(0, (t - fadeStart) / (fadeEnd - fadeStart)));
  return role === "out" ? Math.cos((x * Math.PI) / 2) : Math.sin((x * Math.PI) / 2);
}

// Sampled curves for GainNode.setValueCurveAtTime.
export function gainCurve(fadeStart, fadeEnd, role, points = 128) {
  const out = new Array(points);
  for (let i = 0; i < points; i++) {
    const t = fadeStart + ((fadeEnd - fadeStart) * i) / (points - 1);
    out[i] = gainAt(t, fadeStart, fadeEnd, role);
  }
  return out;
}

// Gain applied to a track's waveform at mix-time t, given the overlap zone.
// Track A (first) fades out across the zone; track B (second) fades in.
export function trackGainAt(t, overlap, role) {
  if (!overlap) return 1;
  if (role === "out") {
    if (t < overlap.start) return 1;
    return gainAt(t, overlap.start, overlap.end, "out");
  }
  if (t > overlap.end) return 1;
  return gainAt(t, overlap.start, overlap.end, "in");
}
