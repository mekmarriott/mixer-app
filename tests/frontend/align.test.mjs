// Alignment: hard beat-grid snapping and score-relative marker sizing
// (P4-16, P4-20, P4-22, P4-23, P4-24).
import test from "node:test";
import assert from "node:assert/strict";
import {
  markerToOffset, bestMarker, snapOffset, snapOffsetTo, nearestBeat,
  quantizeToBeats, beatsBetween, markerSizePx, MARKER_RADIUS_S,
  dropOffset, DEFAULT_OVERLAP_S, placementOffset,
} from "../../frontend/js/align.js";

const marker = (a, b, score) => ({ a_start_s: a, b_start_s: b, score });
// 128 BPM -> a beat every 0.46875 s.
const BPM = 128;
const BEAT = 60 / BPM;
const grid = Array.from({ length: 200 }, (_, i) => +(i * BEAT).toFixed(6));

test("P4-16: on drop, offset snaps to the highest-scoring marker", () => {
  const ms = [marker(30, 2, 0.4), marker(48, 3, 0.9), marker(60, 5, 0.7)];
  assert.equal(bestMarker(ms).score, 0.9);
  assert.equal(snapOffset(ms), 45);
});

test("P4-16: snap handles an empty marker list", () => {
  assert.equal(snapOffset([]), 0);
  assert.equal(snapOffset(null), 0);
  assert.equal(bestMarker([]), null);
});

test("markerToOffset never returns a negative offset", () => {
  assert.equal(markerToOffset(marker(2, 30, 0.5)), 0);
});

test("P4-16: a marker's offset is where TRACK 2 starts, not where A exits", () => {
  // The transition begins 48s into A and 3s into B, so B has to start 45s in
  // for its entry to land on A's exit. This is the position the marker arrow
  // is drawn at and the position a drop lands on — if they disagreed, a track
  // would never appear to land on a marker.
  assert.equal(markerToOffset(marker(48, 3, 0.9)), 45);
});

test("P4-16: dropOffset places a dropped track on the best marker", () => {
  const ms = [marker(30, 2, 0.4), marker(48, 3, 0.9), marker(60, 5, 0.7)];
  assert.equal(dropOffset(ms, 60), 45);
  // Identical to what a drag would snap to, so drop and drag agree.
  assert.equal(dropOffset(ms, 60), snapOffsetTo(45, ms, [], 128));
});

test("P4-16: with NO markers a drop overlaps the previous track's tail", () => {
  // snapOffset would return 0 here, stacking the new track exactly on its
  // predecessor — indistinguishable from "the snap did not happen".
  assert.equal(snapOffset([]), 0);
  assert.equal(dropOffset([], 60), 60 - DEFAULT_OVERLAP_S);
  assert.equal(dropOffset(null, 90), 90 - DEFAULT_OVERLAP_S);
});

test("P4-16: the markerless fallback never goes negative on a short track", () => {
  assert.equal(dropOffset([], 5), 0);
  assert.equal(dropOffset([], 0), 0);
  assert.equal(dropOffset([], undefined), 0);
});

test("P4-23: a placement away from any marker lands on the nearest beat", () => {
  // 20.3s sits between beats 43 (20.156) and 44 (20.625).
  const out = snapOffsetTo(20.3, [], grid);
  assert.equal(out, nearestBeat(20.3, grid));
  assert.ok(Math.abs(out - 20.156250) < 1e-6);
  // On-grid by construction: an exact multiple of the beat.
  assert.ok(Math.abs((out / BEAT) - Math.round(out / BEAT)) < 1e-9);
});

test("P4-23: NO placement can leave beats misaligned", () => {
  // Sweep the whole drag range; every result must sit on a beat.
  for (let t = 0; t < 40; t += 0.037) {
    const out = snapOffsetTo(t, [], grid);
    const beats = out / BEAT;
    assert.ok(Math.abs(beats - Math.round(beats)) < 1e-6,
      `t=${t} resolved to ${out}, which is ${beats} beats — off grid`);
  }
});

test("P4-23: a marker within reach wins over the plain beat grid", () => {
  const ms = [marker(20.0, 0, 0.9)];          // offset 20.0
  const out = snapOffsetTo(20.3, ms, grid);   // 0.3s away, inside the radius
  assert.equal(out, 20.0);
});

test("P4-24: a deliberate placement is not dragged onto a distant marker", () => {
  const ms = [marker(20.0, 0, 0.9)];
  const proposed = 20.0 + MARKER_RADIUS_S + 0.5;   // well outside the radius
  const out = snapOffsetTo(proposed, ms, grid);
  assert.notEqual(out, 20.0);
  // It goes to the nearest beat instead — never further than half a beat.
  assert.ok(Math.abs(out - proposed) <= BEAT / 2 + 1e-9);
});

test("P4-22: placement is not restricted to markers — any beat is reachable", () => {
  const ms = [marker(20.0, 0, 0.9)];
  const reached = new Set();
  for (let t = 30; t < 36; t += 0.05) reached.add(snapOffsetTo(t, ms, grid));
  // Many distinct non-marker positions are available in a 6-second span.
  assert.ok(reached.size > 8, `only ${reached.size} positions reachable`);
  assert.ok(!reached.has(20.0));
});

test("quantizeToBeats makes off-grid positions unrepresentable", () => {
  assert.ok(Math.abs(quantizeToBeats(1.0, BPM) - 2 * BEAT) < 1e-9);
  assert.equal(quantizeToBeats(5.3, 0), 5.3);       // no bpm: unchanged
  // Relative to an origin, so a gap between two tracks quantizes cleanly.
  assert.ok(Math.abs(quantizeToBeats(10 + 1.0, BPM, 10) - (10 + 2 * BEAT)) < 1e-9);
});

test("beatsBetween converts a gap to whole beats", () => {
  assert.equal(beatsBetween(4 * BEAT, BPM), 4);
  assert.equal(beatsBetween(4 * BEAT + 0.01, BPM), 4);
  assert.equal(beatsBetween(0, BPM), 0);
  assert.equal(beatsBetween(5, 0), 0);
});

test("snapOffsetTo falls back to bpm quantization with no explicit grid", () => {
  const out = snapOffsetTo(1.0, [], null, BPM);
  assert.ok(Math.abs(out - 2 * BEAT) < 1e-9);
});

test("P4-20: marker size is scaled RELATIVE to the candidates on screen", () => {
  // A real measured pair: five markers spanning 0.781..0.805.
  const scores = [0.8054, 0.7886, 0.7884, 0.7841, 0.7811];
  const sizes = scores.map((s) => markerSizePx(s, 10, 26, scores));

  // On an absolute 0..1 scale these differ by 0.4px — visually identical.
  const absolute = scores.map((s) => markerSizePx(s, 10, 26));
  assert.ok(Math.max(...absolute) - Math.min(...absolute) < 1,
    "absolute scaling should collapse this band (that is the bug)");

  // Relative scaling spans the full range, so the ranking is legible.
  assert.equal(Math.min(...sizes), 10);
  assert.equal(Math.max(...sizes), 26);
  // Order is preserved: better score, bigger arrow.
  const byScore = [...scores].sort((a, b) => a - b);
  const bySize = byScore.map((s) => markerSizePx(s, 10, 26, scores));
  for (let i = 1; i < bySize.length; i++) assert.ok(bySize[i] >= bySize[i - 1]);
});

test("P4-20: all-equal scores render uniformly rather than dividing by zero", () => {
  const scores = [0.5, 0.5, 0.5];
  const sizes = scores.map((s) => markerSizePx(s, 10, 26, scores));
  assert.ok(sizes.every(Number.isFinite));
  assert.deepEqual(sizes, [18, 18, 18]);
});

test("P4-20: size is bounded and clamps out-of-range scores", () => {
  const scores = [0, 1];
  assert.equal(markerSizePx(-1, 10, 26, scores), 10);
  assert.equal(markerSizePx(2, 10, 26, scores), 26);
  // A single marker has nothing to compare against: absolute scale.
  assert.equal(markerSizePx(0.5, 10, 26, [0.5]), 18);
});


test("P4-16c: placement takes the best marker AT OR AFTER the legal floor", () => {
  // Offsets are 20, 45 and 56. The top-scoring one (20) would reach back into
  // the track two before this one, so 45 is the earliest legal start.
  const ms = [marker(22, 2, 0.95), marker(50, 5, 0.80), marker(60, 4, 0.70)];
  const out = placementOffset(ms, { minDelta: 45, prevDuration: 60 });

  // It lands on the best LEGAL marker (offset 45, score 0.80) — not the
  // highest-scoring one, and not a bare clamp of it forward to the floor.
  assert.equal(out, 45);
  assert.notEqual(out, 20);
});

test("P4-16c: with no floor, placement is just the best marker", () => {
  const ms = [marker(22, 2, 0.95), marker(50, 5, 0.80)];
  assert.equal(placementOffset(ms, { minDelta: 0, prevDuration: 60 }), 20);
  assert.equal(placementOffset(ms, { prevDuration: 60 }), dropOffset(ms, 60));
});

test("P4-16c: when NO marker is legal, placement is legal and still musical", () => {
  const ms = [marker(10, 2, 0.95), marker(12, 3, 0.9)];   // offsets 8 and 9
  const out = placementOffset(ms, { minDelta: 40, prevDuration: 60 });
  // Both constraints hold: at or after the floor, and a plain tail crossfade
  // rather than sitting exactly on a limit derived from a neighbour.
  assert.ok(out >= 40);
  assert.equal(out, 60 - DEFAULT_OVERLAP_S);
});

test("P4-16c: a floor later than the tail default still wins", () => {
  const ms = [marker(10, 2, 0.95)];
  assert.equal(placementOffset(ms, { minDelta: 52, prevDuration: 60 }), 52);
});

test("P4-16c: the floor always wins over the markerless fallback", () => {
  // prevDuration - DEFAULT_OVERLAP_S would be 44, but 50 is the legal floor.
  assert.equal(placementOffset([], { minDelta: 50, prevDuration: 60 }), 50);
  // And with a floor of 0 it is the ordinary tail overlap.
  assert.equal(placementOffset([], { minDelta: 0, prevDuration: 60 }),
               60 - DEFAULT_OVERLAP_S);
});

test("P4-16c: a placement is never below the floor, for any marker set", () => {
  const sets = [
    [], null,
    [marker(5, 1, 0.9)],
    [marker(5, 1, 0.9), marker(80, 2, 0.95), marker(40, 30, 0.99)],
  ];
  for (const set of sets) {
    for (const floor of [0, 10, 45, 100]) {
      const out = placementOffset(set, { minDelta: floor, prevDuration: 60 });
      assert.ok(out >= floor - 1e-9,
        `set=${JSON.stringify(set)} floor=${floor} -> ${out}`);
    }
  }
});
