// The playhead is a drag target, independent of track dragging (P4-06, P4-07).
//
// It used to be reachable only by clicking bare canvas — which, once a mix has
// tracks, is nowhere. The top strip is now a scrub ruler and the handle is
// grabbable anywhere it sits.
import { test, expect } from "@playwright/test";
import {
  bootApp, addFirstTrack, addSecondTrack, readClock, timelineBox,
} from "./helpers.mjs";

const RULER_Y = 10;            // inside the ruler strip (timeline.js RULER_H = 30)

async function scrub(page, fromFrac, toFrac, { y = RULER_Y, steps = 15 } = {}) {
  const box = await timelineBox(page);
  await page.mouse.move(box.x + box.width * fromFrac, box.y + y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * toFrac, box.y + y, { steps });
  await page.mouse.up();
}

test.describe("playhead", () => {
  test("PH-01 the playhead can be dragged while paused, over a track", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    await expect(page.locator("#time-now")).toHaveText("0:00");
    await scrub(page, 0.15, 0.6);

    const landed = await readClock(page, "#time-now");
    expect(landed).toBeGreaterThan(0);

    // Paused means paused: it must not start creeping after release.
    await page.waitForTimeout(1200);
    expect(await readClock(page, "#time-now")).toBe(landed);
  });

  test("PH-02 the playhead can be dragged WHILE PLAYING and playback resumes", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    await page.locator("#btn-play").click();
    await expect.poll(() => readClock(page, "#time-now"), { timeout: 15_000 })
      .toBeGreaterThanOrEqual(1);

    // Drag backwards, so "it resumed" cannot be confused with "it kept playing".
    await scrub(page, 0.60, 0.20);
    const landed = await readClock(page, "#time-now");

    await expect.poll(() => readClock(page, "#time-now"), { timeout: 15_000 })
      .toBeGreaterThan(landed);
  });

  test("PH-03 the ruler does not hijack track dragging", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const before = await readClock(page, "#time-total");
    const box = await timelineBox(page);

    // Same x, but down in the waveform band: this must move the TRACK.
    await page.mouse.move(box.x + box.width * 0.9, box.y + box.height * 0.7);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.96, box.y + box.height * 0.7, { steps: 12 });
    await page.mouse.up();

    await expect.poll(() => readClock(page, "#time-total")).toBeGreaterThan(before);
  });

  test("PH-04 dragging a track does not move the playhead", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    await scrub(page, 0.15, 0.35);
    const cursor = await readClock(page, "#time-now");

    const box = await timelineBox(page);
    await page.mouse.move(box.x + box.width * 0.9, box.y + box.height * 0.7);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.95, box.y + box.height * 0.7, { steps: 10 });
    await page.mouse.up();
    await page.waitForTimeout(400);

    // The two drags are independent controls.
    expect(await readClock(page, "#time-now")).toBe(cursor);
  });
});
