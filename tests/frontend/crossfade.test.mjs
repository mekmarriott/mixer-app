// Crossfade gain across a chain of tracks (P4-02, P4-25).
import test from "node:test";
import assert from "node:assert/strict";
import { gainAt, gainCurve, trackGainAt } from "../../frontend/js/crossfade.js";

const zone = (start, end) => ({ start, end });

test("P4-02: equal-power crossfade — a^2 + b^2 == 1 across the whole zone", () => {
  const z = zone(10, 20);
  for (let t = 10; t <= 20; t += 0.1) {
    const out = trackGainAt(t, { outgoing: z });
    const inn = trackGainAt(t, { incoming: z });
    assert.ok(Math.abs(out * out + inn * inn - 1) < 1e-9, `power dipped at t=${t}`);
  }
});

test("P4-02: fade-out is monotonic 1 -> 0; fade-in is monotonic 0 -> 1", () => {
  let prevOut = Infinity, prevIn = -Infinity;
  for (let t = 10; t <= 20; t += 0.25) {
    const out = gainAt(t, 10, 20, "out");
    const inn = gainAt(t, 10, 20, "in");
    assert.ok(out <= prevOut + 1e-12, "fade-out not monotonic");
    assert.ok(inn >= prevIn - 1e-12, "fade-in not monotonic");
    prevOut = out; prevIn = inn;
  }
  assert.ok(Math.abs(gainAt(10, 10, 20, "out") - 1) < 1e-9);
  assert.ok(Math.abs(gainAt(20, 10, 20, "out")) < 1e-9);
  assert.ok(Math.abs(gainAt(10, 10, 20, "in")) < 1e-9);
  assert.ok(Math.abs(gainAt(20, 10, 20, "in") - 1) < 1e-9);
});

test("P4-25: outside every zone a track plays and draws at full gain", () => {
  assert.equal(trackGainAt(5, {}), 1);
  assert.equal(trackGainAt(5, { outgoing: zone(10, 20) }), 1);   // before its fade
  assert.equal(trackGainAt(25, { incoming: zone(10, 20) }), 1);  // after its fade
});

test("P4-25: an INTERIOR track fades in, holds, then fades out", () => {
  const overlaps = { incoming: zone(10, 20), outgoing: zone(40, 50) };
  assert.ok(Math.abs(trackGainAt(10, overlaps)) < 1e-9);        // silent at entry
  assert.ok(Math.abs(trackGainAt(20, overlaps) - 1) < 1e-9);    // full once in
  assert.equal(trackGainAt(30, overlaps), 1);                   // held between
  assert.ok(Math.abs(trackGainAt(40, overlaps) - 1) < 1e-9);    // full at exit start
  assert.ok(Math.abs(trackGainAt(50, overlaps)) < 1e-9);        // silent at exit
});

test("P4-25: a chain keeps constant power at every junction", () => {
  // Three tracks, two junctions. At any instant only neighbours sound.
  const j1 = zone(45, 60), j2 = zone(90, 105);
  for (const [t, expected] of [[50, [j1]], [95, [j2]]]) {
    const a = trackGainAt(t, { outgoing: expected[0] });
    const b = trackGainAt(t, { incoming: expected[0] });
    assert.ok(Math.abs(a * a + b * b - 1) < 1e-9, `power dipped at junction t=${t}`);
  }
  // A track between its two junctions is alone and at full gain.
  assert.equal(trackGainAt(75, { incoming: j1, outgoing: j2 }), 1);
});

test("P4-25: touching zones compose as a product rather than clipping", () => {
  // Pathological but representable: a very short track whose fades overlap.
  const g = trackGainAt(15, { incoming: zone(10, 20), outgoing: zone(15, 25) });
  assert.ok(g > 0 && g < 1);
  assert.ok(Math.abs(g - Math.SQRT1_2) < 1e-9);
});

test("gainCurve samples the composed gain, so a double fade stays correct", () => {
  const overlaps = { incoming: zone(0, 10), outgoing: zone(10, 20) };
  const curve = gainCurve(0, 20, overlaps, 41);
  assert.equal(curve.length, 41);
  assert.ok(Math.abs(curve[0]) < 1e-9);                 // silent at the start
  assert.ok(Math.abs(curve[20] - 1) < 1e-9);            // peak in the middle
  assert.ok(Math.abs(curve.at(-1)) < 1e-9);             // silent at the end
  assert.ok(curve.every((v) => v >= -1e-12 && v <= 1 + 1e-12));
});

test("a degenerate zone never divides by zero", () => {
  assert.equal(gainAt(5, 10, 10, "out"), 1);
  assert.equal(gainAt(5, 20, 10, "in"), 1);
  assert.ok(Number.isFinite(trackGainAt(10, { incoming: zone(10, 10) })));
});
