// Alignment logic (testing-document P4-16, P4-20, P4-22..P4-24).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  bestMarker, markerToOffset, snapOffset, magneticOffset, markerSizePx,
  MARKER_RADIUS_S, BEAT_RADIUS_S,
} from "../../frontend/js/align.js";

const markers = [
  { a_start_s: 49.0, b_start_s: 0.3, score: 0.81 },
  { a_start_s: 47.0, b_start_s: 4.2, score: 0.79 },
  { a_start_s: 30.0, b_start_s: 2.0, score: 0.55 },
];

test("P4-16: on drop, offset snaps to the highest-scoring marker", () => {
  assert.equal(bestMarker(markers).score, 0.81);
  // offset places B's entry point exactly on A's exit point
  assert.equal(snapOffset(markers), 49.0 - 0.3);
});

test("P4-16: snap handles empty marker list (offset 0)", () => {
  assert.equal(snapOffset([]), 0);
  assert.equal(snapOffset(null), 0);
});

test("markerToOffset never returns negative offsets", () => {
  assert.equal(markerToOffset({ a_start_s: 1.0, b_start_s: 5.0, score: 0.5 }), 0);
});

test("P4-22: drag far from every attractor is unchanged (free placement)", () => {
  const proposed = 20.0; // >MARKER_RADIUS_S from every marker offset, no beats
  assert.equal(magneticOffset(proposed, markers, []), proposed);
});

test("P4-23: magnetic pull engages inside the marker radius", () => {
  const target = markerToOffset(markers[0]); // 48.7
  const proposed = target + MARKER_RADIUS_S * 0.4;
  const pulled = magneticOffset(proposed, markers, []);
  // moved toward the target, but not past it
  assert.ok(Math.abs(pulled - target) < Math.abs(proposed - target));
  assert.ok((pulled - target) * (proposed - target) >= 0);
});

test("P4-23: pull is strongest at the center (full snap)", () => {
  const target = markerToOffset(markers[0]);
  assert.ok(Math.abs(magneticOffset(target + 0.001, markers, []) - target) < 0.001);
});

test("P4-23: beat-grid attractors engage when no marker is near", () => {
  const beats = [10.0, 10.5, 11.0];
  const pulled = magneticOffset(10.5 + BEAT_RADIUS_S * 0.3, [], beats);
  assert.ok(Math.abs(pulled - 10.5) < BEAT_RADIUS_S * 0.3);
});

test("P4-24: pull never overrides deliberate placement outside the radius", () => {
  const target = markerToOffset(markers[0]);
  const proposed = target + MARKER_RADIUS_S + 0.01; // just past the edge
  assert.equal(magneticOffset(proposed, markers, []), proposed);
});

test("P4-24: pull eases off toward the radius edge (not a hard lock)", () => {
  const target = markerToOffset(markers[0]);
  const nearEdge = target + MARKER_RADIUS_S * 0.95;
  const pulled = magneticOffset(nearEdge, markers, []);
  // Near the edge, the offset moves only slightly — placement intent respected.
  assert.ok(Math.abs(pulled - nearEdge) < MARKER_RADIUS_S * 0.15);
  assert.notEqual(pulled, target);
});

test("P4-20: marker size is strictly increasing with score and bounded", () => {
  const sizes = [0, 0.25, 0.5, 0.75, 1].map((s) => markerSizePx(s));
  for (let i = 1; i < sizes.length; i++) assert.ok(sizes[i] > sizes[i - 1]);
  assert.equal(markerSizePx(0), 10);
  assert.equal(markerSizePx(1), 26);
  assert.equal(markerSizePx(2), 26);   // clamped
  // Proportionality: equal score steps produce equal size steps.
  const d1 = markerSizePx(0.5) - markerSizePx(0.25);
  const d2 = markerSizePx(0.75) - markerSizePx(0.5);
  assert.ok(Math.abs(d1 - d2) < 1e-9);
});
