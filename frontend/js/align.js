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

// Magnetic pull (P4-23): given a proposed offset (from a drag), pull it
// toward nearby attractors. Marker attractors have a wider radius and a
// stronger pull than beat attractors; outside every radius the offset is
// unchanged (free drag, P4-22).
export const MARKER_RADIUS_S = 1.25;
export const BEAT_RADIUS_S = 0.18;

export function magneticOffset(proposed, markers, beatGrid) {
  let best = { dist: Infinity, target: proposed, radius: 0 };
  for (const m of markers || []) {
    const target = markerToOffset(m);
    const d = Math.abs(proposed - target);
    if (d <= MARKER_RADIUS_S && d < best.dist) {
      best = { dist: d, target, radius: MARKER_RADIUS_S };
    }
  }
  if (best.dist === Infinity) {
    for (const bt of beatGrid || []) {
      const d = Math.abs(proposed - bt);
      if (d <= BEAT_RADIUS_S && d < best.dist) {
        best = { dist: d, target: bt, radius: BEAT_RADIUS_S };
      }
    }
  }
  if (best.dist === Infinity) return proposed;
  // Smooth pull: full snap at the center, easing off toward the radius edge.
  const strength = 1 - (best.dist / best.radius) ** 2;
  return proposed + (best.target - proposed) * strength;
}

// Marker arrow sizing (P4-21): px height proportional to score.
export function markerSizePx(score, minPx = 10, maxPx = 26) {
  const s = Math.max(0, Math.min(1, score));
  return minPx + (maxPx - minPx) * s;
}
