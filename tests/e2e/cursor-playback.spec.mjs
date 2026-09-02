// Player cursor + transport. These assert against real WebAudio clock time,
// which is exactly why they cannot live in the node:test suite.
import { test, expect } from "@playwright/test";
import { bootApp, addFirstTrack, readClock, timelineBox } from "./helpers.mjs";

test.describe("player cursor", () => {
  test("P4-06 clicking the track window seeks playback to that time", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);

    const total = await readClock(page, "#time-total");
    expect(total).toBeGreaterThan(0);
    await expect(page.locator("#time-now")).toHaveText("0:00");

    // The viewport spans the whole mix at this point, so x is a linear map
    // onto mix time: clicking halfway should land halfway.
    const box = await timelineBox(page);
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.7);

    await expect
      .poll(() => readClock(page, "#time-now"))
      .toBeGreaterThan(0);
    const now = await readClock(page, "#time-now");
    expect(Math.abs(now - total * 0.5)).toBeLessThanOrEqual(2);

    // Seeking again to a different point moves it again — not a one-shot.
    await page.mouse.click(box.x + box.width * 0.8, box.y + box.height * 0.7);
    const later = await readClock(page, "#time-now");
    expect(later).toBeGreaterThan(now);
    expect(Math.abs(later - total * 0.8)).toBeLessThanOrEqual(2);
  });

  test("P4-07 cursor advances while playing and holds when paused", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);

    await page.locator("#btn-play").click();

    // Playing: the readout must actually advance against the audio clock.
    await expect.poll(() => readClock(page, "#time-now"), { timeout: 15_000 })
      .toBeGreaterThanOrEqual(2);
    const whilePlaying = await readClock(page, "#time-now");

    await page.locator("#btn-play").click();
    const atPause = await readClock(page, "#time-now");
    expect(atPause).toBeGreaterThanOrEqual(whilePlaying);

    // Paused: it must stop dead, not merely slow down.
    await page.waitForTimeout(2500);
    expect(await readClock(page, "#time-now")).toBe(atPause);

    // Resuming picks up from where it stopped rather than restarting.
    await page.locator("#btn-play").click();
    await expect.poll(() => readClock(page, "#time-now"), { timeout: 15_000 })
      .toBeGreaterThan(atPause);
  });
});
