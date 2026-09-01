// Nav bar viewport math (testing-document P4-08, P4-09).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createViewport, setTotal, pan, resizeEdge, clamp,
  timeToPx, pxToTime, MIN_VIEW_S,
} from "../../frontend/js/navbar.js";

test("P4-09: dragging the segment pans without changing zoom", () => {
  const vp = createViewport(120);
  vp.dur = 40; clamp(vp);
  pan(vp, 15);
  assert.equal(vp.start, 15);
  assert.equal(vp.dur, 40);          // zoom unchanged
  pan(vp, -100);
  assert.equal(vp.start, 0);         // clamped at the left edge
  assert.equal(vp.dur, 40);
});

test("P4-09: pan clamps at the right edge of the mix", () => {
  const vp = createViewport(120);
  vp.dur = 40; clamp(vp);
  pan(vp, 999);
  assert.equal(vp.start, 80);        // 120 - 40
  assert.equal(vp.dur, 40);
});

test("P4-08: dragging the right edge resizes -> zooms proportionally", () => {
  const vp = createViewport(120);
  vp.dur = 40; vp.start = 20; clamp(vp);
  resizeEdge(vp, "right", 20);       // widen by 20s => zoom out
  assert.equal(vp.dur, 60);
  assert.equal(vp.start, 20);
  resizeEdge(vp, "right", -40);      // narrow => zoom in
  assert.equal(vp.dur, 20);
});

test("P4-08: dragging the left edge zooms and anchors the right edge", () => {
  const vp = createViewport(120);
  vp.dur = 40; vp.start = 20; clamp(vp);
  const end = vp.start + vp.dur;     // 60
  resizeEdge(vp, "left", 10);
  assert.equal(vp.start + vp.dur, end);
  assert.equal(vp.start, 30);
  assert.equal(vp.dur, 30);
});

test("zoom respects the minimum view duration", () => {
  const vp = createViewport(120);
  vp.dur = 10; clamp(vp);
  resizeEdge(vp, "right", -999);
  assert.equal(vp.dur, MIN_VIEW_S);
  resizeEdge(vp, "left", 999);
  assert.equal(vp.dur, MIN_VIEW_S);
});

test("viewport never exceeds the mix bounds after total changes", () => {
  const vp = createViewport(60);
  vp.start = 30; vp.dur = 30; clamp(vp);
  setTotal(vp, 40);                  // mix shrank
  assert.ok(vp.start + vp.dur <= 40);
  assert.ok(vp.start >= 0);
});

test("time<->pixel mapping round-trips inside the viewport", () => {
  const vp = createViewport(120);
  vp.start = 20; vp.dur = 40;
  const W = 800;
  for (const t of [20, 33.3, 47.9, 60]) {
    assert.ok(Math.abs(pxToTime(vp, timeToPx(vp, t, W), W) - t) < 1e-9);
  }
  assert.equal(timeToPx(vp, 20, W), 0);
  assert.equal(timeToPx(vp, 60, W), W);
});
