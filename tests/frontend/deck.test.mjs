// Suggested song deck (testing-document P4-12, P4-13).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  rankRecommendations, scorePercent, pieAngleDeg, piePath,
} from "../../frontend/js/deck.js";

test("P4-12: deck order is strictly descending by match score", () => {
  const shuffled = [
    { track_id: "b", score: 0.64 },
    { track_id: "a", score: 0.87 },
    { track_id: "c", score: 0.41 },
  ];
  const ranked = rankRecommendations(shuffled);
  assert.deepEqual(ranked.map((r) => r.track_id), ["a", "b", "c"]);
  for (let i = 1; i < ranked.length; i++) {
    assert.ok(ranked[i].score <= ranked[i - 1].score);
  }
});

test("P4-12: ranking tolerates empty/absent input", () => {
  assert.deepEqual(rankRecommendations([]), []);
  assert.deepEqual(rankRecommendations(null), []);
});

test("P4-13: numeric percentage matches the score", () => {
  assert.equal(scorePercent(0.87), 87);
  assert.equal(scorePercent(0.005), 1);
  assert.equal(scorePercent(0), 0);
  assert.equal(scorePercent(1), 100);
  assert.equal(scorePercent(1.5), 100);   // clamped
  assert.equal(scorePercent(-1), 0);      // clamped
});

test("P4-13: pie fill angle is proportional to the same score", () => {
  assert.equal(pieAngleDeg(0), 0);
  assert.equal(pieAngleDeg(0.25), 90);
  assert.equal(pieAngleDeg(0.5), 180);
  assert.equal(pieAngleDeg(0.87), 0.87 * 360);
  assert.equal(pieAngleDeg(1), 360);
});

test("P4-13: pie path geometry — wedge endpoint sits on the circle at the score angle", () => {
  const r = 8, cx = 9, cy = 9;
  for (const score of [0.1, 0.3, 0.49, 0.51, 0.75, 0.9]) {
    const d = piePath(score, r, cx, cy);
    // Path ends with "A ... {x} {y} Z" — parse the arc endpoint.
    const m = d.match(/A [\d. ]+ (\d) 1 ([\d.-]+) ([\d.-]+) Z$/);
    assert.ok(m, `no arc in path for score ${score}`);
    const [_, large, xs, ys] = m;
    const x = parseFloat(xs), y = parseFloat(ys);
    // Endpoint lies on the circle...
    assert.ok(Math.abs(Math.hypot(x - cx, y - cy) - r) < 1e-6);
    // ...at the angle implied by the score (12 o'clock start, clockwise).
    const ang = ((pieAngleDeg(score) - 90) * Math.PI) / 180;
    assert.ok(Math.abs(x - (cx + r * Math.cos(ang))) < 1e-6);
    assert.ok(Math.abs(y - (cy + r * Math.sin(ang))) < 1e-6);
    // Large-arc flag flips at 50%.
    assert.equal(Number(large), score > 0.5 ? 1 : 0, `large-arc at ${score}`);
  }
});

test("P4-13: pie path edge cases — empty at 0, full circle at 1", () => {
  assert.equal(piePath(0), "");
  assert.match(piePath(1), /A 8 8 0 1 1/);   // full-circle arc
});
