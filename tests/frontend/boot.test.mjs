// Warmup presentation logic (INF-04, client side). Pure module, no DOM.
import test from "node:test";
import assert from "node:assert/strict";
import {
  isReady, isFailed, progressPercent, statusMessage, statusDetail, pollDelayMs,
} from "../../frontend/js/boot.js";

const warming = { phase: "ingesting", ready: false, done: 3, total: 9,
                  message: "Analyzing Midnight Grid (4/9)", elapsed_s: 4.2,
                  percent: 33 };
const ready = { phase: "ready", ready: true, done: 9, total: 9,
                message: "Ready", elapsed_s: 12.5, percent: 100 };
const failed = { phase: "failed", ready: false, error: "OperationalError: boom" };

test("INF-04: readiness requires both the flag and the phase", () => {
  assert.equal(isReady(ready), true);
  assert.equal(isReady(warming), false);
  assert.equal(isReady(null), false);
  // A stale flag without the matching phase must not unblock the app.
  assert.equal(isReady({ ready: true, phase: "ingesting" }), false);
});

test("INF-04: failure is distinguished from still-warming", () => {
  assert.equal(isFailed(failed), true);
  assert.equal(isFailed(warming), false);
  assert.equal(isFailed(null), false);
});

test("INF-04: progress uses the server percent when present", () => {
  assert.equal(progressPercent(warming), 33);
  assert.equal(progressPercent(ready), 100);
});

test("INF-04: progress falls back to done/total and clamps", () => {
  assert.equal(progressPercent({ done: 1, total: 4 }), 25);
  assert.equal(progressPercent({ done: 9, total: 0 }), 0);
  assert.equal(progressPercent({ percent: 140 }), 100);
  assert.equal(progressPercent({ percent: -5 }), 0);
  assert.equal(progressPercent(null), 0);
});

test("INF-04: message prefers the server's own phase text", () => {
  assert.equal(statusMessage(warming), "Analyzing Midnight Grid (4/9)");
  assert.match(statusMessage(null), /Connecting/);
  // A failure surfaces the actual error, not a generic phase label.
  assert.equal(statusMessage(failed), "OperationalError: boom");
});

test("INF-04: message falls back to a phase label when none is supplied", () => {
  assert.equal(statusMessage({ phase: "precomputing" }), "Precomputing waveforms");
});

test("INF-04: detail shows counts and elapsed time, and omits noise", () => {
  assert.equal(statusDetail(warming), "3/9 · 4.2s");
  assert.equal(statusDetail({ total: 0, elapsed_s: 2 }), "2s");
  assert.equal(statusDetail({}), "");
  assert.match(statusDetail(failed), /server log/);
});

test("INF-04: poll backoff grows but stays bounded", () => {
  const delays = [0, 1, 2, 5, 20].map((i) => pollDelayMs(i));
  // Monotonic, so a long ingest is not hammered...
  for (let i = 1; i < delays.length; i++) {
    assert.ok(delays[i] >= delays[i - 1], `delay ${i} regressed`);
  }
  // ...starts responsive, and never stalls the overlay.
  assert.ok(delays[0] <= 300);
  assert.ok(delays.at(-1) <= 2000);
  assert.equal(pollDelayMs(-3), pollDelayMs(0));
});
