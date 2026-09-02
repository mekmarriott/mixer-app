// The opening screen: warmup gating, browse-by-genre, and the promise that
// selecting the first track costs no new computation.
import { test, expect } from "@playwright/test";
import { bootApp, addFirstTrack, DRAGGABLE_ROW } from "./helpers.mjs";

test.describe("zero state", () => {
  test("ZS-01 deck browses by genre with a bounded number of tracks each", async ({ page }) => {
    await bootApp(page);

    await expect(page.locator("#deck-sub")).toContainText("Browse by genre");

    const groups = page.locator("#deck .genre-group");
    expect(await groups.count()).toBeGreaterThan(0);

    const perGenre = await page.evaluate(async () =>
      (await (await fetch("/api/deck")).json()).per_genre);

    for (let i = 0; i < (await groups.count()); i++) {
      const group = groups.nth(i);
      // Every group is labelled and none exceeds the cap.
      await expect(group.locator(".genre-name")).not.toBeEmpty();
      const rows = await group.locator(".deck-row").count();
      expect(rows).toBeGreaterThan(0);
      expect(rows).toBeLessThanOrEqual(perGenre);
    }

    // Nothing is ranked yet — with no track chosen there is nothing to score.
    await expect(page.locator("#deck .deck-score span")).toHaveCount(0);
  });

  test("ZS-02 the opening page load makes no per-track waveform requests", async ({ page }) => {
    // Land on a fresh, empty mix FIRST. Boot resumes the most recently edited
    // mix, and resuming one that has tracks legitimately fetches their
    // waveforms — that is not what this test is about. Measure the load that
    // lands in the zero state.
    await bootApp(page);

    const requests = [];
    page.on("request", (r) => requests.push(new URL(r.url()).pathname + new URL(r.url()).search));
    await page.reload();
    await expect(page.locator("#deck .deck-row").first()).toBeVisible({ timeout: 60_000 });
    await page.waitForLoadState("networkidle");

    // Waveforms are precomputed at server startup and inlined into /api/deck.
    // A request-per-row here was the old boot fan-out that triggered API-01.
    const waveformCalls = requests.filter((u) => u.includes("/waveform"));
    expect(waveformCalls, `unexpected waveform requests:\n${waveformCalls.join("\n")}`)
      .toEqual([]);

    // And the rows still render their envelope, so this is not a silent loss.
    const drawn = await page.evaluate(() =>
      [...document.querySelectorAll("#deck .deck-row canvas")]
        .filter((c) => c.width > 0 && c.height > 0).length);
    expect(drawn).toBeGreaterThan(0);
  });

  test("ZS-03 pair analysis only starts once track 1 is selected", async ({ page }) => {
    await bootApp(page);

    const requests = [];
    page.on("request", (r) => requests.push(new URL(r.url()).pathname));

    // Before any selection: no transitions endpoint has been touched.
    expect(requests.filter((u) => u.startsWith("/api/transitions"))).toEqual([]);

    await addFirstTrack(page);

    // Selecting track 1 fetches recommendations, but still no pair analysis —
    // that waits for track 2.
    expect(requests.some((u) => u.includes("/recommendations"))).toBe(true);
    expect(requests.filter((u) => u.startsWith("/api/transitions"))).toEqual([]);
  });

  test("ZS-04 status endpoint reports readiness and pool bounds", async ({ page }) => {
    await bootApp(page);

    const status = await page.evaluate(async () =>
      (await (await fetch("/api/status")).json()));

    expect(status.ready).toBe(true);
    expect(status.phase).toBe("ready");
    expect(status.percent).toBe(100);

    // Admission is bounded, and stays under the engine's connection ceiling
    // when it advertises one. SQLite reports null (one connection per thread,
    // no intrinsic ceiling), so admission itself is the bound there.
    expect(status.db.max_concurrency).toBeGreaterThan(0);
    expect(status.db.peak_in_flight).toBeLessThanOrEqual(status.db.max_concurrency);
    if (status.db.connection_ceiling !== null) {
      expect(status.db.max_concurrency).toBeLessThan(status.db.connection_ceiling);
    }
  });

  test("ZS-05 the boot overlay is dismissed once the catalog is ready", async ({ page }) => {
    await bootApp(page);
    // The overlay exists in the markup but must not be covering the app.
    await expect(page.locator("#boot-overlay")).toBeHidden();
    await expect(page.locator(DRAGGABLE_ROW).first()).toBeVisible();
  });
});
