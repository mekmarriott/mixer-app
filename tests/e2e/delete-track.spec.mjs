// Removing a track from a mix (DEL-01..05).
//
// The required ripple: the track immediately after the deleted one drops onto
// the BEST transition point with its NEW predecessor — the two were never
// neighbours, so its old gap is meaningless. Everything after that keeps its
// own delta, so those transitions survive exactly as they were.
//
// Assertions are written against whichever index was actually removed rather
// than a hardcoded one: the canvas maps time to pixels through the viewport,
// and pinning a click to an index would test the arithmetic in the test.
import { test, expect } from "@playwright/test";
import { bootApp, addFirstTrack, addSecondTrack, dragRowToTimeline,
         DRAGGABLE_ROW, timelineBox, expectNoFailedWrites,
         settleTo } from "./helpers.mjs";

/** The persisted chain, which is the thing that has to be correct. */
async function chain(page) {
  return page.evaluate(async () => {
    const mixes = await (await fetch("/api/mixes")).json();
    const mix = await (await fetch(`/api/mixes/${mixes[0].id}`)).json();
    return mix.tracks.map((t) => ({
      id: t.track_id, beats: t.delta_beats,
      off: +t.offset_s.toFixed(2), dur: +t.duration_s.toFixed(2),
    }));
  });
}

/**
 * Build the longest chain the catalog allows, up to `max`.
 *
 * Waits for the SERVER to reflect each add before continuing: the chain is
 * saved asynchronously, and reading it early yields a stale mix — which then
 * makes every click coordinate derived from it land on the wrong track.
 */
async function settled(page, expected) {
  // Not expect.poll: a rejected save must surface as the rejection, not as a
  // timeout ten seconds later that says nothing about why.
  await settleTo(async () => (await chain(page)).length, expected, "the chain");
  return chain(page);
}

async function buildChain(page, max = 4) {
  await addFirstTrack(page);
  await settled(page, 1);
  await addSecondTrack(page);
  let ch = await settled(page, 2);

  while (ch.length < max && await page.locator(DRAGGABLE_ROW).count()) {
    await dragRowToTimeline(page, page.locator(DRAGGABLE_ROW).first());
    await expect(page.locator("#attributions span")).toHaveCount(ch.length + 1);
    ch = await settled(page, ch.length + 1);
  }

  // Every add must have been accepted by the server. A rejected save leaves
  // the chain shorter than the UI shows, and the failure then surfaces as a
  // confusing timeout further down instead of at its cause.
  expectNoFailedWrites();

  // Reload so the viewport spans the whole mix. Adding a track pans the view
  // to reveal the new junction, which leaves the visible window offset — and
  // every click coordinate below is derived from "x maps linearly across the
  // whole mix", which is only true when the whole mix is in view.
  await page.reload();
  await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
  await page.waitForLoadState("networkidle");
  return settled(page, ch.length);
}

/** Click inside the visible span of the track at `index`, then press Delete. */
async function deleteAt(page, index, ch) {
  const box = await timelineBox(page);
  const total = Math.max(...ch.map((t) => t.off + t.dur));
  const start = ch[index].off;
  const end = Math.min(start + ch[index].dur,
                       ch[index + 1] ? ch[index + 1].off : Infinity);
  const mid = (start + end) / 2;
  await page.mouse.click(box.x + (mid / total) * box.width, box.y + box.height * 0.72);
  await page.waitForTimeout(200);
  await page.keyboard.press("Delete");
  return settled(page, ch.length - 1);
}

test.describe("deleting a track", () => {
  test("DEL-01 a middle track is removed and the chain heals", async ({ page }) => {
    await bootApp(page);
    const before = await buildChain(page);
    test.skip(before.length < 3, "catalog cannot build a 3-track chain");

    const after = await deleteAt(page, 1, before);

    // Exactly one track gone, order otherwise preserved.
    const removed = before.find((t) => !after.some((a) => a.id === t.id));
    expect(removed, "nothing was removed").toBeTruthy();
    expect(after.map((t) => t.id))
      .toEqual(before.filter((t) => t.id !== removed.id).map((t) => t.id));

    const gone = before.findIndex((t) => t.id === removed.id);
    expect(gone, "expected a middle or head track, not the tail")
      .toBeLessThan(before.length - 1);

    // The successor re-snapped: it now follows a track it never followed.
    const successorId = before[gone + 1].id;
    const succAfter = after.find((t) => t.id === successorId);
    expect(succAfter).toBeTruthy();

    // Everything AFTER the successor kept its own delta exactly.
    for (let i = gone + 2; i < before.length; i++) {
      const b = before[i];
      const a = after.find((t) => t.id === b.id);
      expect(a.beats, `track ${b.id} lost its transition to its predecessor`)
        .toBe(b.beats);
    }
  });

  test("DEL-02 the head can be deleted and the mix restarts at zero", async ({ page }) => {
    await bootApp(page);
    const before = await buildChain(page, 3);
    test.skip(before.length < 2, "catalog cannot build a 2-track chain");

    const after = await deleteAt(page, 0, before);

    expect(after.length).toBe(before.length - 1);
    expect(after[0].id).toBe(before[1].id);
    // A leading delta is an absolute start, so the new head anchors the mix.
    expect(after[0].beats).toBe(0);
    expect(after[0].off).toBe(0);

    // Tracks after the new head keep their spacing.
    for (let i = 2; i < before.length; i++) {
      const a = after.find((t) => t.id === before[i].id);
      expect(a.beats).toBe(before[i].beats);
    }
  });

  test("DEL-03 the tail can be deleted, leaving earlier transitions intact", async ({ page }) => {
    await bootApp(page);
    const before = await buildChain(page, 3);
    test.skip(before.length < 2, "catalog cannot build a 2-track chain");

    const after = await deleteAt(page, before.length - 1, before);

    expect(after.map((t) => t.id)).toEqual(before.slice(0, -1).map((t) => t.id));
    // Nothing follows the tail, so nothing re-snaps: every delta is untouched.
    for (const b of before.slice(0, -1)) {
      expect(after.find((t) => t.id === b.id).beats).toBe(b.beats);
    }
  });

  test("DEL-04 deletion persists across a reload", async ({ page }) => {
    await bootApp(page);
    const before = await buildChain(page, 3);
    test.skip(before.length < 2, "catalog cannot build a 2-track chain");

    const after = await deleteAt(page, 0, before);

    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
    await page.waitForLoadState("networkidle");

    expect(await chain(page)).toEqual(after);
    await expect(page.locator("#attributions span")).toHaveCount(after.length);
  });

  test("DEL-05 deleting the only track returns to the zero state", async ({ page }) => {
    await bootApp(page);
    await addFirstTrack(page);
    const before = await chain(page);
    expect(before.length).toBe(1);

    await deleteAt(page, 0, before);

    await expect(page.locator("#deck-sub")).toContainText("Browse by genre");
    // The zero state renders a placeholder line rather than an empty footer.
    await expect(page.locator("#attributions")).toContainText(
      "Attribution for loaded tracks appears here");
    await expect(page.locator("#drop-hint")).toBeVisible();
    await expect(page.locator("#btn-play")).toBeDisabled();
  });
});
