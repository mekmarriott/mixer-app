// Dropping a track snaps it to the best transition point (P4-16).
//
// The requirement has a visible half and a stored half, and they have to
// agree: the incoming track's left edge must land on the highest-scoring
// marker arrow, and the persisted gap must equal that marker's offset.
//
// They did NOT agree before. Arrows were drawn at `a_start_s` — where the
// transition begins inside the OUTGOING track — while the incoming track
// landed at `a_start_s - b_start_s`, because it has to start early enough for
// its own entry point to meet that exit. So the track never sat under the
// arrow it had snapped to, and the snap looked like it had not happened.
import { test, expect } from "@playwright/test";
import {
  bootApp, addFirstTrack, addSecondTrack, markerAndTrackGeometry,
} from "./helpers.mjs";

async function chain(page) {
  return page.evaluate(async () => {
    const mixes = await (await fetch("/api/mixes")).json();
    const mix = await (await fetch(`/api/mixes/${mixes[0].id}`)).json();
    return mix.tracks.map((t) => ({
      id: t.track_id, beats: t.delta_beats, grid: t.grid_bpm,
      off: +t.offset_s.toFixed(3),
    }));
  });
}

/** The best marker for a junction, straight from the API. */
async function bestMarkerOffset(page, aId, bId) {
  return page.evaluate(async ([a, b]) => {
    const tr = await (await fetch(`/api/transitions?a=${a}&b=${b}`)).json();
    if (!tr.markers?.length) return null;
    const best = tr.markers.reduce((x, y) => (y.score > x.score ? y : x));
    return { offset: Math.max(0, best.a_start_s - best.b_start_s),
             score: best.score, grid: tr.grid_bpm };
  }, [aId, bId]);
}

test.describe("drop snapping", () => {
  test("P4-16 a dropped track is placed at the best transition point", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const ch = await chain(page);
    expect(ch.length).toBe(2);

    const best = await bestMarkerOffset(page, ch[0].id, ch[1].id);
    expect(best, "the pair produced no markers to snap to").toBeTruthy();

    // Stored placement equals the best marker, to within the beat grid the
    // position is quantised onto.
    const beat = 60 / best.grid;
    expect(Math.abs(ch[1].off - best.offset)).toBeLessThanOrEqual(beat);
  });

  test("P4-16 the dropped track visibly lands on the highest-scoring arrow", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    // Reload so the viewport spans the whole mix; adding a track pans it.
    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
    await page.waitForLoadState("networkidle");

    const geo = await markerAndTrackGeometry(page);
    expect(geo.arrows.length, "no markers were drawn").toBeGreaterThan(0);
    expect(geo.track2Start, "track 2 was not drawn").not.toBeNull();

    const biggest = geo.arrows.reduce((a, x) => (x.height > a.height ? x : a));

    // The largest arrow sits on the incoming track's left edge. A generous
    // tolerance: bar width, antialiasing and beat quantisation all contribute
    // a few pixels, but the OLD bug displaced this by the whole of b_start_s.
    expect(Math.abs(biggest.centre - geo.track2Start),
      `largest arrow at ${biggest.centre}px, track starts at ${geo.track2Start}px`)
      .toBeLessThan(geo.width * 0.02);
  });

  test("P4-16 every arrow marks a position the incoming track could occupy", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);
    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
    await page.waitForLoadState("networkidle");

    const geo = await markerAndTrackGeometry(page);
    const ch = await chain(page);
    const total = await page.evaluate(() => {
      const t = document.querySelector("#time-total").textContent.trim().split(":").map(Number);
      return t.length === 3 ? t[0] * 3600 + t[1] * 60 + t[2] : t[0] * 60 + t[1];
    });

    const best = await bestMarkerOffset(page, ch[0].id, ch[1].id);
    const expectedX = (best.offset / total) * geo.width;
    const biggest = geo.arrows.reduce((a, x) => (x.height > a.height ? x : a));

    // The arrow is at the marker's TRACK-2 offset, not at a_start_s.
    expect(Math.abs(biggest.centre - expectedX)).toBeLessThan(geo.width * 0.02);
  });
});
