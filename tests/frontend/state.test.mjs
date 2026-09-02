// Mix state: chained tracks, rigid ripple, pairwise overlaps
// (P4-17, P4-18, P4-18b, P4-19).
import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_TRACKS, createMix, canAddTrack, addTrack, insertTrack, removeTrack,
  setDelta, setOffset, offsets, offsetOf, totalDuration,
  overlapAt, overlapZones, overlapsFor, formatTime,
} from "../../frontend/js/state.js";

const trk = (id, duration = 60) => ({ id, name: id, duration });

/** A chain of `n` tracks each starting `delta` after the previous. */
function chain(n, delta = 45, duration = 60) {
  const mix = createMix();
  for (let i = 0; i < n; i++) addTrack(mix, trk(`t${i}`, duration), i === 0 ? 0 : delta);
  return mix;
}

test("P4-18: a mix accepts up to 100 tracks", () => {
  assert.equal(MAX_TRACKS, 100);
  const mix = chain(MAX_TRACKS);
  assert.equal(mix.tracks.length, 100);
  assert.equal(canAddTrack(mix), false);

  const res = addTrack(mix, trk("overflow"), 45);
  assert.equal(res.ok, false);
  assert.match(res.reason, /100/);           // user-facing reason names the cap
  assert.equal(mix.tracks.length, 100);
});

test("P4-18: a long mix spans hours and reports the time correctly", () => {
  const mix = chain(100, 150, 180);          // 100 x 3min, 2.5min apart
  assert.ok(totalDuration(mix) > 4 * 3600 - 1, "expected a multi-hour mix");
  assert.match(formatTime(totalDuration(mix)), /^\d+:\d\d:\d\d$/);
});

test("offsets derive from the chain; deltas are what is stored", () => {
  const mix = chain(4, 45);
  assert.deepEqual(offsets(mix), [0, 45, 90, 135]);
  assert.equal(offsetOf(mix, "t2"), 90);
  // Nothing stores an absolute position.
  assert.ok(mix.tracks.every((t) => t.delta !== undefined && t.offset === undefined));
});

test("P4-18b: moving a track ripples the whole tail rigidly", () => {
  const mix = chain(5, 45);
  const before = offsets(mix);

  setDelta(mix, "t2", 60);                   // +15s later
  const after = offsets(mix);

  assert.deepEqual(after.slice(0, 2), before.slice(0, 2));   // upstream untouched
  for (let i = 2; i < 5; i++) {
    assert.equal(after[i], before[i] + 15, `track ${i} did not ripple`);
  }
});

test("P4-18b: rippling preserves every downstream transition unchanged", () => {
  const mix = chain(5, 45);
  // Key by junction, not array position: an edit can close a zone entirely,
  // after which the remaining zones would shift index and mask a regression.
  const widths = (m) => Object.fromEntries(
    overlapZones(m).map((z) => [z.from, z.end - z.start]));
  const before = widths(mix);

  setDelta(mix, "t2", 55);           // narrow junction 1, keep it non-empty
  const after = widths(mix);

  assert.equal(after[0], before[0], "upstream transition changed");
  assert.notEqual(after[1], before[1], "edited junction did not change");
  // Everything downstream of the edit keeps its exact shape — the point of a
  // rigid ripple.
  assert.equal(after[2], before[2]);
  assert.equal(after[3], before[3]);
});

test("P4-18b: a ripple can close a transition entirely without disturbing later ones", () => {
  const mix = chain(5, 45);          // 60s tracks, 45s apart -> 15s overlaps
  setDelta(mix, "t2", 60);           // exactly abutting: junction 1 disappears
  const zones = overlapZones(mix);
  assert.deepEqual(zones.map((z) => z.from), [0, 2, 3]);
  assert.ok(zones.every((z) => z.end - z.start === 15));
});

test("P4-18b: inserting in the middle shifts the tail by the new gap", () => {
  const mix = chain(4, 45);
  const before = offsets(mix);

  insertTrack(mix, 2, trk("inserted"), 30);
  const after = offsets(mix);

  assert.equal(mix.tracks[2].id, "inserted");
  assert.deepEqual(after.slice(0, 2), before.slice(0, 2));
  assert.equal(after[2], before[1] + 30);
  // Everything after keeps its own spacing, carried along by the insert.
  assert.equal(after[3] - after[2], 45);
  assert.equal(after[4] - after[3], 45);
});

test("P4-18b: deleting in the middle closes the gap and keeps later spacing", () => {
  const mix = chain(5, 45);
  removeTrack(mix, "t2");
  const after = offsets(mix);

  assert.equal(mix.tracks.length, 4);
  assert.deepEqual(after, [0, 45, 90, 135]);   // t3/t4 rode back by 45
  assert.deepEqual(mix.tracks.map((t) => t.id), ["t0", "t1", "t3", "t4"]);
});

test("removing the head does not drag the mix backwards", () => {
  const mix = chain(3, 45);
  removeTrack(mix, "t0");
  // The new head starts the mix; a leading delta is an absolute start.
  assert.deepEqual(offsets(mix), [0, 45]);
});

test("P4-17: overlaps exist only between neighbours", () => {
  const mix = chain(3, 45, 60);              // each pair overlaps 15s
  const zones = overlapZones(mix);
  assert.equal(zones.length, 2);
  assert.deepEqual(zones.map((z) => [z.from, z.to]), [[0, 1], [1, 2]]);
  assert.equal(zones[0].start, 45);
  assert.equal(zones[0].end, 60);
});

test("P4-17: no overlap when a track starts after its predecessor ends", () => {
  const mix = createMix();
  addTrack(mix, trk("a", 60), 0);
  addTrack(mix, trk("b", 60), 90);           // starts 30s after a ends
  assert.equal(overlapAt(mix, 0), null);
  assert.deepEqual(overlapZones(mix), []);
});

test("an interior track carries both an incoming and an outgoing zone", () => {
  const mix = chain(3, 45, 60);
  const { incoming, outgoing } = overlapsFor(mix, 1);
  assert.ok(incoming && outgoing);
  assert.deepEqual([incoming.from, incoming.to], [0, 1]);
  assert.deepEqual([outgoing.from, outgoing.to], [1, 2]);

  // The ends of the mix have only one side.
  assert.equal(overlapsFor(mix, 0).incoming, null);
  assert.equal(overlapsFor(mix, 2).outgoing, null);
});

test("P4-19: overlap boundaries move when a track is dragged", () => {
  const mix = chain(2, 45, 60);
  const before = overlapAt(mix, 0);
  setDelta(mix, "t1", 30);
  const after = overlapAt(mix, 0);
  assert.equal(after.start, 30);
  assert.ok(after.end - after.start > before.end - before.start);
});

test("setOffset is a delta edit expressed in absolute terms", () => {
  const mix = chain(3, 45);
  setOffset(mix, "t2", 200);
  assert.equal(offsetOf(mix, "t2"), 200);
  assert.equal(mix.tracks[2].delta, 155);    // 200 - t1's start (45)
  assert.equal(offsetOf(mix, "t1"), 45);     // upstream untouched
});

test("deltas and offsets clamp at zero; unknown ids are rejected", () => {
  const mix = chain(2, 45);
  setDelta(mix, "t1", -10);
  assert.equal(mix.tracks[1].delta, 0);
  assert.equal(setDelta(mix, "nope", 5), false);
  assert.equal(setOffset(mix, "nope", 5), false);
  assert.equal(removeTrack(mix, "nope"), false);
});

test("total duration spans to the end of the last-finishing track", () => {
  const mix = chain(3, 45, 60);
  assert.equal(totalDuration(mix), 150);
});

test("removeTrack frees a slot at the cap", () => {
  const mix = chain(MAX_TRACKS);
  assert.equal(canAddTrack(mix), false);
  removeTrack(mix, "t5");
  assert.equal(canAddTrack(mix), true);
  assert.equal(addTrack(mix, trk("new"), 45).ok, true);
});

test("formatTime renders m:ss, and h:mm:ss once the mix is that long", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3600), "1:00:00");
  assert.equal(formatTime(7825), "2:10:25");
  assert.equal(formatTime(-5), "0:00");
  assert.equal(formatTime(NaN), "0:00");
});
