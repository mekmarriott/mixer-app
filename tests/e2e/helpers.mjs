// Shared helpers for the browser suite.
//
// The app has no test hooks and no framework — these helpers drive it exactly
// as a user does (real events, real canvas, real WebAudio) and read state back
// only through what the UI actually renders. Nothing here reaches into module
// internals, so a passing test means the visible product works.
import { expect } from "@playwright/test";

// timeline.js COLORS, as RGB triples.
export const COLORS = {
  track1: [255, 79, 163],   // #ff4fa3 magenta
  track2: [79, 168, 255],   // #4fa8ff blue
  marker: [255, 194, 75],   // #ffc24b gold
};

// timeline.js MARKER_LANE_H (device px at deviceScaleFactor 1).
export const MARKER_LANE_H = 30;

const DECK_ROW = "#deck .deck-row";
export const DRAGGABLE_ROW = `${DECK_ROW}[draggable="true"]`;

/**
 * Load the app on a FRESH, empty mix.
 *
 * Mixes are persisted and boot resumes the most recently edited one, so
 * without this every test would inherit whatever the previous test built.
 * Creating a mix makes it the most recent; the reload then resumes it, which
 * also exercises the real resume path rather than a test-only shortcut.
 */
export async function bootApp(page, { fresh = true } = {}) {
  await page.goto("/");
  await expect(page.locator(DECK_ROW).first()).toBeVisible({ timeout: 60_000 });

  if (fresh) {
    await page.evaluate(() =>
      fetch("/api/mixes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Untitled Mix" }),
      }).then((r) => r.json()));
    await page.reload();
    await expect(page.locator(DECK_ROW).first()).toBeVisible({ timeout: 60_000 });
  }

  // Let boot traffic settle so tests that count requests start from silence.
  await page.waitForLoadState("networkidle");
}

/**
 * Perform a real HTML5 drag-and-drop from a deck row onto the track window.
 *
 * Playwright's mouse API cannot trigger native HTML5 drag events, so the
 * sequence is dispatched with one shared DataTransfer. The app's own
 * `dragstart` handler is what writes the track id into it — this exercises the
 * production handlers rather than bypassing them.
 */
export async function dragRowToTimeline(page, rowLocator) {
  await rowLocator.evaluate((row) => {
    const dt = new DataTransfer();
    const opts = { dataTransfer: dt, bubbles: true, cancelable: true };
    row.dispatchEvent(new DragEvent("dragstart", opts));
    const wrap = document.querySelector(".track-window-wrap");
    wrap.dispatchEvent(new DragEvent("dragover", opts));
    wrap.dispatchEvent(new DragEvent("drop", opts));
    row.dispatchEvent(new DragEvent("dragend", opts));
  });
}

/** Drop the first mixable deck row in as track 1; returns its visible name. */
export async function addFirstTrack(page) {
  const row = page.locator(DRAGGABLE_ROW).first();
  const name = await row.locator(".deck-name").innerText();
  await dragRowToTimeline(page, row);
  // Track 1 is loaded once the hint clears and transport is armed.
  await expect(page.locator("#drop-hint")).toBeHidden();
  await expect(page.locator("#btn-play")).toBeEnabled();
  // The deck re-renders into recommendations.
  await expect(page.locator("#deck-sub")).toContainText("Suggested next tracks");
  await page.waitForLoadState("networkidle");
  return name;
}

/** Drop the top-ranked recommendation in as track 2; returns its visible name. */
export async function addSecondTrack(page) {
  const row = page.locator(DRAGGABLE_ROW).first();
  const name = await row.locator(".deck-name").innerText();
  await dragRowToTimeline(page, row);
  // Wait on DURABLE state, not the snap toast: the toast auto-hides after
  // 2.6 s, so a slow drop turns it into a race. Two attribution entries only
  // appear once track 2 is fully in the mix.
  await expect(page.locator("#attributions span")).toHaveCount(2);
  await page.waitForLoadState("networkidle");
  return name;
}

/** Boot and build the full two-track mixing state. */
export async function setupTwoTrackMix(page) {
  await bootApp(page);
  const first = await addFirstTrack(page);
  const second = await addSecondTrack(page);
  return { first, second };
}

/** "1:23" -> 83 */
export async function readClock(page, selector) {
  const txt = (await page.locator(selector).innerText()).trim();
  const [m, s] = txt.split(":").map(Number);
    return m * 60 + s;
}

/**
 * Classify the timeline canvas by colour.
 *
 * Returns per-band pixel counts for each track colour and the distinct x
 * columns carrying a marker arrow. Only near-opaque pixels count, which
 * excludes antialiased edges, the 7%-alpha overlap wash and the 25%-alpha
 * marker-lane divider.
 */
export async function sampleTimeline(page) {
  return page.evaluate(({ colors, laneH }) => {
    const canvas = document.querySelector("#timeline");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const { width: W, height: H } = canvas;
    const data = ctx.getImageData(0, 0, W, H).data;

    const near = (r, g, b, c) =>
      Math.abs(r - c[0]) < 30 && Math.abs(g - c[1]) < 30 && Math.abs(b - c[2]) < 30;

    const out = { width: W, height: H, track1: 0, track2: 0, markerColumns: [] };
    const markerCols = new Set();
    // Arrows sit above the lane divider; stay clear of it.
    const laneBottom = laneH - 3;

    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4;
        if (data[i + 3] < 200) continue;
        const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
        if (y < laneBottom) {
          if (near(r, g, b, colors.marker)) markerCols.add(x);
        } else if (y >= laneH) {
          if (near(r, g, b, colors.track1)) out.track1++;
          else if (near(r, g, b, colors.track2)) out.track2++;
        }
      }
    }
    out.markerColumns = [...markerCols].sort((a, b) => a - b);
    return out;
  }, { colors: COLORS, laneH: MARKER_LANE_H });
}

/**
 * Sample the timeline once a frame has actually been painted.
 *
 * Drawing is requestAnimationFrame-driven, so sampling immediately after a drop
 * can catch an empty canvas on a loaded machine — that is a test race, not a
 * product bug. Poll until the waveform appears rather than assuming one frame
 * has elapsed.
 */
export async function sampleTimelineWhenDrawn(page, { expectTrack2 = false } = {}) {
  let last = null;
  await expect.poll(async () => {
    last = await sampleTimeline(page);
    return expectTrack2 ? Math.min(last.track1, last.track2) : last.track1;
  }, { message: "timeline never painted a waveform" }).toBeGreaterThan(0);
  return last;
}

/** Collapse marker x-columns into arrows (columns within `gap` px are one arrow). */
export function countMarkerClusters(columns, gap = 6) {
  if (!columns.length) return 0;
  let clusters = 1;
  for (let i = 1; i < columns.length; i++) {
    if (columns[i] - columns[i - 1] > gap) clusters++;
  }
  return clusters;
}

/** Bounding box of the timeline canvas, for pointer geometry. */
export async function timelineBox(page) {
  const box = await page.locator("#timeline").boundingBox();
  if (!box) throw new Error("timeline canvas has no box");
  return box;
}
