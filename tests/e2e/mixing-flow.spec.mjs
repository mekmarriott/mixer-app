// Deck -> drop -> two-track overlay. Covers the testing-document items that
// need a real browser: native drag-and-drop, canvas colour, marker rendering
// and track-2 attribution.
import { test, expect } from "@playwright/test";
import {
  bootApp, addFirstTrack, addSecondTrack, dragRowToTimeline,
  sampleTimeline, sampleTimelineWhenDrawn, countMarkerClusters, DRAGGABLE_ROW,
  COLORS, readClock, expectNoFailedWrites,
} from "./helpers.mjs";

test.describe("deck and two-track mixing state", () => {
  test("P4-14 deck rows are draggable and dropping one enters the mixing state", async ({ page }) => {
    await bootApp(page);

    // Every row in the opening deck is usable: tracks that cannot be mixed
    // (ND licence, or ingestion not finished) are omitted rather than shown
    // disabled, so there is nothing here that would refuse a drag.
    const rows = page.locator("#deck .deck-row");
    expect(await rows.count()).toBeGreaterThan(0);
    await expect(page.locator('#deck .deck-row[draggable="false"]')).toHaveCount(0);
    await expect(rows.first()).toHaveAttribute("draggable", "true");

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
    // A drop must never propose a placement the server refuses: the best
    // marker can reach back into the second-nearest predecessor.
    expectNoFailedWrites();
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

test.describe("transition markers across a chain", () => {
  test("P4-21b every junction keeps its own markers, not just the newest", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    await addSecondTrack(page);

    const twoTrack = await sampleTimelineWhenDrawn(page, { expectTrack2: true });
    const firstJunction = countMarkerClusters(twoTrack.markerColumns);
    expect(firstJunction).toBeGreaterThan(0);

    // Add a third track: the new junction gets markers, and — the regression
    // this guards — the first junction must not lose its own.
    if (!(await page.locator(DRAGGABLE_ROW).count())) test.skip();
    await dragRowToTimeline(page, page.locator(DRAGGABLE_ROW).first());
    await expect(page.locator("#attributions span")).toHaveCount(3);

    // Reload so the viewport spans the whole mix and both junctions are drawn.
    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
    await page.waitForLoadState("networkidle");

    const threeTrack = await sampleTimelineWhenDrawn(page, { expectTrack2: true });
    const allJunctions = countMarkerClusters(threeTrack.markerColumns);

    // Arrow COUNT is the wrong measure: a junction shows at most MARKER_TOP_N
    // candidates, they can overlap on screen, and some fall outside the
    // viewport. The property that actually distinguishes "both junctions have
    // markers" from "only the newest does" is that markers appear in two
    // well-separated places along the mix.
    expect(allJunctions).toBeGreaterThan(0);
    const cols = threeTrack.markerColumns;
    const span = cols[cols.length - 1] - cols[0];
    expect(span, "markers are clustered at a single junction")
      .toBeGreaterThan(threeTrack.width * 0.25);

    // Two groups: somewhere there is a gap far larger than the spacing between
    // candidates within one junction.
    let biggestGap = 0;
    for (let i = 1; i < cols.length; i++) {
      biggestGap = Math.max(biggestGap, cols[i] - cols[i - 1]);
    }
    expect(biggestGap, "no separation between junction marker groups")
      .toBeGreaterThan(threeTrack.width * 0.1);
  });
});
