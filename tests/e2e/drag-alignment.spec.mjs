// Dragging track 2 on the x-axis, and the architectural promise that dragging
// costs nothing on the wire.
import { test, expect } from "@playwright/test";
import { bootApp, addFirstTrack, addSecondTrack, readClock, timelineBox } from "./helpers.mjs";

/** Drag track 2 horizontally by `dx` CSS px using real pointer events. */
async function dragTrackTwo(page, dx, { steps = 20 } = {}) {
  const box = await timelineBox(page);
  // Track 2 sits at the tail of the mix and is the only track under the far
  // right of the window, so this grabs it unambiguously.
  const startX = box.x + box.width * 0.92;
  const y = box.y + box.height * 0.7;
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(startX + dx, y, { steps });
  await page.mouse.up();
}

test.describe("free-drag alignment", () => {
  test("P4-10 track 2 can be dragged along the x-axis and mix timing follows", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const totalBefore = await readClock(page, "#time-total");
    expect(totalBefore).toBeGreaterThan(0);

    // Drag right: track 2 starts later, so the whole mix gets longer.
    await dragTrackTwo(page, 120);
    await expect.poll(() => readClock(page, "#time-total")).toBeGreaterThan(totalBefore);
    const totalRight = await readClock(page, "#time-total");

    // Drag back left: it shortens again. Repositioning is not one-way.
    await dragTrackTwo(page, -90);
    await expect.poll(() => readClock(page, "#time-total")).toBeLessThan(totalRight);
  });

  test("P4-04 dragging issues zero server round-trips", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    // Everything the drag needs was preloaded at drop time; from here the
    // network must go completely silent (project plan, Phase 4).
    const requests = [];
    page.on("request", (r) => requests.push(r.url()));

    await dragTrackTwo(page, 100);
    await dragTrackTwo(page, -60);
    await page.waitForTimeout(500);

    expect(requests, `unexpected requests during drag:\n${requests.join("\n")}`).toEqual([]);
  });
});
