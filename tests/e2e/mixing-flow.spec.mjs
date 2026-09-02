// Deck -> drop -> two-track overlay. Covers the testing-document items that
// need a real browser: native drag-and-drop, canvas colour, marker rendering
// and track-2 attribution.
import { test, expect } from "@playwright/test";
import {
  bootApp, addFirstTrack, addSecondTrack, dragRowToTimeline,
  sampleTimeline, sampleTimelineWhenDrawn, countMarkerClusters, DRAGGABLE_ROW,
  COLORS, readClock,
} from "./helpers.mjs";

test.describe("deck and two-track mixing state", () => {
  test("P4-14 deck rows are draggable and dropping one enters the mixing state", async ({ page }) => {
    await bootApp(page);

    // Every mixable row advertises itself as draggable; ND rows must not.
    const rows = page.locator("#deck .deck-row");
    await expect(rows.first()).toHaveAttribute("draggable", "true");
    const ndRows = page.locator('#deck .deck-row[draggable="false"]');
    for (let i = 0; i < (await ndRows.count()); i++) {
      await expect(ndRows.nth(i)).toHaveAttribute("title", /ND-licensed/);
    }

    await addFirstTrack(page);
    await addSecondTrack(page);

    // The app's own confirmation that the drop landed on the best marker.
    await expect(page.locator("#toast")).toContainText("Snapped to best transition");

    // The mixing state is live: two attributions, markers on the canvas, and
    // the transport armed.
    await expect(page.locator("#attributions span")).toHaveCount(2);
    await expect(page.locator("#btn-play")).toBeEnabled();
    const px = await sampleTimelineWhenDrawn(page, { expectTrack2: true });
    expect(px.markerColumns.length).toBeGreaterThan(0);
  });

  test("P4-15 track 2 renders in a colour distinct from track 1", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);

    // One track: only magenta on the canvas.
    let px = await sampleTimelineWhenDrawn(page);
    expect(px.track1).toBeGreaterThan(0);
    expect(px.track2).toBe(0);

    await addSecondTrack(page);

    // Two tracks: both colours present simultaneously, and they are the two
    // mandated hues rather than one shared colour.
    px = await sampleTimelineWhenDrawn(page, { expectTrack2: true });
    expect(px.track1).toBeGreaterThan(0);
    expect(px.track2).toBeGreaterThan(0);
    expect(COLORS.track1).not.toEqual(COLORS.track2);
  });

  test("P4-21 multiple transition markers render simultaneously", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const { markerColumns } = await sampleTimelineWhenDrawn(page, { expectTrack2: true });
    const arrows = countMarkerClusters(markerColumns);
    // ui-requirements shows three; the backend surfaces MARKER_TOP_N (5).
    expect(arrows).toBeGreaterThanOrEqual(2);
    expect(arrows).toBeLessThanOrEqual(5);
  });

  test("P4-18 a mix chains well past the old two-track cap", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    // The v1 limit of two was lifted (ui-requirements.md §Overlay): keep
    // adding while the deck still offers a compatible track.
    let added = 2;
    while (added < 6 && await page.locator(DRAGGABLE_ROW).count()) {
      await dragRowToTimeline(page, page.locator(DRAGGABLE_ROW).first());
      added++;
      await expect(page.locator("#attributions span")).toHaveCount(added);
    }

    expect(added, "expected the chain to grow beyond two tracks").toBeGreaterThan(2);
    // Every track in the chain is attributed, not just the first two.
    await expect(page.locator("#attributions span")).toHaveCount(added);
  });

  test("P4-18b adding a track ripples the mix longer, never shorter", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    const oneTrack = await readClock(page, "#time-total");

    await addSecondTrack(page);
    const twoTracks = await readClock(page, "#time-total");
    expect(twoTracks).toBeGreaterThan(oneTrack);

    if (await page.locator(DRAGGABLE_ROW).count()) {
      await dragRowToTimeline(page, page.locator(DRAGGABLE_ROW).first());
      await expect(page.locator("#attributions span")).toHaveCount(3);
      // The third track appends to the tail; it cannot shorten the mix.
      await expect.poll(() => readClock(page, "#time-total")).toBeGreaterThan(twoTracks);
    }
  });

  test("P4-27 attribution is displayed for track 2 once added", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);

    await expect(page.locator("#attributions span")).toHaveCount(1);
    await addSecondTrack(page);

    const atts = page.locator("#attributions span");
    await expect(atts).toHaveCount(2);

    // Each carries artist/title text plus a link to that track's own CC deed.
    for (let i = 0; i < 2; i++) {
      const link = atts.nth(i).locator("a");
      await expect(link).toHaveAttribute("href", /creativecommons\.org\/licenses\//);
      await expect(link).toHaveAttribute("rel", /license/);
      expect((await atts.nth(i).innerText()).trim().length).toBeGreaterThan(0);
    }
  });
});
