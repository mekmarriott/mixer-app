// Crossfade curves (testing-document P4-02, P4-25).
//
// P4-25's core claim — the drawn fade equals the audible fade — holds by
// construction: audio.js schedules gainCurve(...) onto GainNodes and
// timeline.js scales waveform bars with trackGainAt(...), both from this one
// module. These tests pin down that shared curve's properties.
import { test } from "node:test";
import assert from "node:assert/strict";
import { gainAt, gainCurve, trackGainAt } from "../../frontend/js/crossfade.js";

const zone = { start: 45, end: 60 };

test("P4-02: equal-power crossfade — a^2 + b^2 == 1 across the whole zone", () => {
  for (let t = 45; t <= 60; t += 0.5) {
    const a = gainAt(t, 45, 60, "out");
    const b = gainAt(t, 45, 60, "in");
    assert.ok(Math.abs(a * a + b * b - 1) < 1e-9, `at t=${t}`);
  }
});

test("P4-02: fade-out is monotonic 1 -> 0; fade-in is monotonic 0 -> 1", () => {
  const out = gainCurve(45, 60, "out", 64);
  const inn = gainCurve(45, 60, "in", 64);
  assert.equal(out[0], 1);
  assert.ok(out[63] < 1e-9);
  assert.equal(inn[0], 0);
  assert.ok(Math.abs(inn[63] - 1) < 1e-9);
  for (let i = 1; i < 64; i++) {
    assert.ok(out[i] <= out[i - 1] + 1e-12);
    assert.ok(inn[i] >= inn[i - 1] - 1e-12);
  }
});

test("P4-25: outside the overlap, each track plays/draws at full gain", () => {
  assert.equal(trackGainAt(10, zone, "out"), 1);   // A before the fade
  assert.equal(trackGainAt(70, zone, "in"), 1);    // B after the fade
});

test("P4-25: inside the overlap, drawn gain follows the same curve", () => {
  const mid = 52.5;
  assert.ok(Math.abs(trackGainAt(mid, zone, "out") - Math.SQRT1_2) < 1e-9);
  assert.ok(Math.abs(trackGainAt(mid, zone, "in") - Math.SQRT1_2) < 1e-9);
  // Track A is fully silent by the end of the zone; B fully up.
  assert.ok(trackGainAt(60, zone, "out") < 1e-9);
  assert.ok(Math.abs(trackGainAt(60, zone, "in") - 1) < 1e-9);
});

test("no overlap zone means no gain shaping at all", () => {
  assert.equal(trackGainAt(30, null, "out"), 1);
  assert.equal(trackGainAt(30, null, "in"), 1);
});

test("degenerate zone (end <= start) never divides by zero", () => {
  assert.equal(gainAt(5, 10, 10, "out"), 1);
  assert.equal(gainAt(5, 10, 10, "in"), 1);
});
