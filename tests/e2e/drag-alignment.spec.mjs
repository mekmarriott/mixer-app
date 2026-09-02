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

  test("P4-04 dragging fetches nothing; it only writes the new position", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const reads = [];
    const writes = [];
    page.on("request", (r) => {
      const path = new URL(r.url()).pathname;
      if (r.method() === "GET") reads.push(path);
      else writes.push(`${r.method()} ${path}`);
    });

    await dragTrackTwo(page, 100);
    await dragTrackTwo(page, -60);
    await page.waitForTimeout(900);

    // The original requirement: everything the drag needs — audio, analysis,
    // waveforms, transition curve — was preloaded at drop time, so a drag
    // FETCHES nothing. That still holds and is the property that keeps
    // dragging responsive.
    expect(reads, `drag fetched from the server:\n${reads.join("\n")}`).toEqual([]);

    // Position IS persisted, deliberately: the write is one row, one column.
    // What must not happen is a request per pointermove — those fire ~60/s, so
    // the drag coalesces and flushes on release.
    expect(writes.length).toBeGreaterThan(0);
    expect(writes.length, `too many writes for two drags:\n${writes.join("\n")}`)
      .toBeLessThanOrEqual(6);
    expect(writes.every((w) => w.startsWith("PATCH /api/mixes/"))).toBe(true);
  });
});
