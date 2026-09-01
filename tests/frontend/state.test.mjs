// Mix state (testing-document P4-17..P4-19).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createMix, addTrack, removeTrack, setOffset, canAddTrack,
  totalDuration, overlapZone, formatTime, MAX_TRACKS,
} from "../../frontend/js/state.js";

const A = { id: "a", name: "A", artist: "x", duration: 60, offset: 0 };
const B = { id: "b", name: "B", artist: "y", duration: 60, offset: 45 };

test("P4-18: a third track cannot be added (max 2, with a user-facing reason)", () => {
  const mix = createMix();
  assert.equal(MAX_TRACKS, 2);
  assert.ok(addTrack(mix, A).ok);
  assert.ok(addTrack(mix, B).ok);
  assert.equal(canAddTrack(mix), false);
  const third = addTrack(mix, { id: "c", duration: 30 });
  assert.equal(third.ok, false);
  assert.match(third.reason, /Two tracks/);
  assert.equal(mix.tracks.length, 2);
});

test("P4-17: overlap zone exists only where both tracks are present", () => {
  const mix = createMix();
  addTrack(mix, A);
  assert.equal(overlapZone(mix), null);          // one track: no overlay state
  addTrack(mix, B);
  assert.deepEqual(overlapZone(mix), { start: 45, end: 60 });
});

test("P4-17: no overlap zone when track 2 starts after track 1 ends", () => {
  const mix = createMix();
  addTrack(mix, A);
  addTrack(mix, { ...B, offset: 80 });
  assert.equal(overlapZone(mix), null);
});

test("P4-19: overlap boundaries adjust when track 2 is dragged", () => {
  const mix = createMix();
  addTrack(mix, A);
  addTrack(mix, B);
  assert.ok(setOffset(mix, "b", 30));
  assert.deepEqual(overlapZone(mix), { start: 30, end: 60 });
  setOffset(mix, "b", 55);
  assert.deepEqual(overlapZone(mix), { start: 55, end: 60 });
});

test("offsets clamp at zero; unknown ids are rejected", () => {
  const mix = createMix();
  addTrack(mix, { ...A });
  setOffset(mix, "a", -10);
  assert.equal(mix.tracks[0].offset, 0);
  assert.equal(setOffset(mix, "nope", 5), false);
});

test("total mix duration spans to the end of the later track", () => {
  const mix = createMix();
  addTrack(mix, A);
  assert.equal(totalDuration(mix), 60);
  addTrack(mix, B);                               // 45 + 60
  assert.equal(totalDuration(mix), 105);
});

test("removeTrack frees a slot for a new drop", () => {
  const mix = createMix();
  addTrack(mix, A);
  addTrack(mix, B);
  assert.ok(removeTrack(mix, "b"));
  assert.equal(canAddTrack(mix), true);
});

test("formatTime renders m:ss", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(-3), "0:00");
});
