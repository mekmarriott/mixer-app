// Alignment — pure logic, no DOM (tests P4-16, P4-22..P4-24).
//
// Design decisions (ui-requirements.md §Resolved):
//  - on drop, track 2 snaps to the highest-scoring marker
//  - free drag is allowed, with a magnetic "pull" toward markers and the
//    beat grid; markers attract more strongly than plain beats

// A marker: {a_start_s, b_start_s, score}. Track 2's timeline offset for a
// marker = the marker's exit point in A minus B's entry point, so that B's
// suggested entry lands exactly on A's exit.
export function markerToOffset(marker) {
  return Math.max(0, marker.a_start_s - marker.b_start_s);
}

export function bestMarker(markers) {
  if (!markers || markers.length === 0) return null;
  return markers.reduce((best, m) => (m.score > best.score ? m : best), markers[0]);
}

// Auto-snap on drop (P4-16): offset for the highest-scoring marker.
export function snapOffset(markers) {
  const m = bestMarker(markers);
  return m ? markerToOffset(m) : 0;
}

// Beat-grid quantization — a HARD constraint, not a preference.
//
// Previously this was a magnetic *pull*: strong near an attractor, absent
// elsewhere, so a track could come to rest between beats and play out of time.
// Beats now cannot be misaligned, because an unaligned position is not
// representable: every placement is quantized to the grid before it is stored.
//
// Markers still win over plain beats when one is in reach, so the
// highest-scoring transitions remain easy to hit — but the fallback is the
// nearest beat rather than wherever the pointer happened to stop.
export const MARKER_RADIUS_S = 1.25;

/** Nearest value in a sorted-or-unsorted grid. Returns `t` if the grid is empty. */
export function nearestBeat(t, beatGrid) {
  if (!beatGrid || !beatGrid.length) return t;
  let best = beatGrid[0];
  let bestD = Math.abs(t - best);
  for (const b of beatGrid) {
    const d = Math.abs(t - b);
    if (d < bestD) { best = b; bestD = d; }
  }
  return best;
}

/**
 * Quantize to a whole number of beats at `bpm`, measured from `origin`.
 *
 * Used when no explicit grid is available (e.g. the gap between two tracks
 * already on a shared BPM grid). Integer beats make misalignment
 * unrepresentable rather than merely unlikely — the same reason the persisted
 * schema stores `delta_beats` as an integer.
 */
export function quantizeToBeats(t, bpm, origin = 0) {
  if (!bpm || bpm <= 0) return t;
  const beat = 60 / bpm;
  return origin + Math.round((t - origin) / beat) * beat;
}

export function beatsBetween(seconds, bpm) {
  if (!bpm || bpm <= 0) return 0;
  return Math.round(seconds / (60 / bpm));
}

/**
 * Resolve a dragged position to a legal one.
 *
 * A marker inside MARKER_RADIUS_S wins outright (they are already beat-aligned
 * by construction — transitions.py snaps window starts to downbeats). Anything
 * else lands on the nearest beat. There is no free placement: the return value
 * is always on the grid.
 */
export function snapOffsetTo(proposed, markers, beatGrid, bpm = null) {
  let bestMarkerTarget = null;
  let bestMarkerDist = Infinity;
  for (const m of markers || []) {
    const target = markerToOffset(m);
    const d = Math.abs(proposed - target);
    if (d <= MARKER_RADIUS_S && d < bestMarkerDist) {
      bestMarkerDist = d;
      bestMarkerTarget = target;
    }
  }
  if (bestMarkerTarget !== null) return Math.max(0, bestMarkerTarget);

  if (beatGrid && beatGrid.length) return Math.max(0, nearestBeat(proposed, beatGrid));
  return Math.max(0, quantizeToBeats(proposed, bpm));
}

// Marker arrow sizing (P4-20): height encodes how good that transition is.
//
// Scoring the SAME pair produces a narrow band — a real pair measured
// 0.781..0.805, a spread of 0.024. Mapped through an absolute 0..1 scale that
// is 22.5px..22.9px: every arrow the same size, and the one thing the marker
// lane exists to communicate invisible.
//
// So the scale is RELATIVE to the set being drawn: the weakest candidate on
// offer sits at minPx, the strongest at maxPx, and the rest spread between.
// The comparison a user actually makes is "which of these is best?", and that
// is what this makes legible. Absolute score stays available on hover.
export function markerSizePx(score, minPx = 10, maxPx = 26, scores = null) {
  const s = Math.max(0, Math.min(1, score));
  if (!scores || scores.length < 2) return minPx + (maxPx - minPx) * s;

  let lo = Infinity, hi = -Infinity;
  for (const v of scores) {
    const c = Math.max(0, Math.min(1, v));
    if (c < lo) lo = c;
    if (c > hi) hi = c;
  }
  // All-equal scores: no ordering to convey, so show them uniformly at the
  // midpoint rather than dividing by zero or implying a ranking.
  if (hi - lo < 1e-9) return (minPx + maxPx) / 2;
  return minPx + (maxPx - minPx) * ((s - lo) / (hi - lo));
}
