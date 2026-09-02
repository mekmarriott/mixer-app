// Regression test for a defect the browser suite found on its first run.
//
// THE DEFECT
// backend/app.py opens ONE sqlite3.Connection with check_same_thread=False and
// shares it across every Flask worker thread. backend/db.py guards writes with
// WRITE_LOCK but takes no lock on reads (get_track / analysis_of /
// variants_for). Concurrent execute() calls on a single connection interleave,
// so reads either raise (sqlite3.InterfaceError -> HTTP 500) or return a
// phantom-empty row that the endpoint reports as HTTP 404 for a track that
// plainly exists.
//
// This is invisible to the unittest suite, which drives Flask's test_client
// single-threaded. It only appears when a real browser opens the page and the
// deck fires one waveform request per row in parallel — measured at roughly
// 15-20% of concurrent reads failing, versus 0% sequentially.
//
// design-document.md §6 asserts "check_same_thread=False + a write lock handles
// Flask's threaded server". The write lock is not sufficient; reads need the
// same serialization, or each thread needs its own connection.
//
// STATUS: test.fail() — this is expected to FAIL until the defect is fixed.
// Playwright reports an error if it starts passing, which is the signal to drop
// this annotation and move the row in docs/automation-test-manifest.md from
// "known defect" to "covered". If it ever passes intermittently instead, the
// race simply did not trigger on that machine — re-run, and see the repro
// command in the manifest.
import { test, expect } from "@playwright/test";
import { bootApp } from "./helpers.mjs";

const BURST = 36;

test.describe("catalog API under concurrent reads", () => {
  test.fail();
  test("API-01 parallel catalog reads all succeed (shared SQLite connection race)", async ({ page }) => {
    await bootApp(page);

    const ids = await page.evaluate(async () =>
      (await (await fetch("/api/tracks")).json()).map((t) => t.id));
    expect(ids.length).toBeGreaterThan(0);

    // Fire the burst from the page so the requests genuinely land on separate
    // Flask worker threads, the same way the deck does at boot.
    const results = await page.evaluate(async ({ ids, burst }) => {
      const urls = Array.from({ length: burst },
        (_, i) => `/api/tracks/${ids[i % ids.length]}/waveform?points=120`);
      const statuses = await Promise.all(
        urls.map((u) => fetch(u).then((r) => ({ url: u, status: r.status }))));
      return statuses.filter((s) => s.status !== 200);
    }, { ids, burst: BURST });

    expect(results, `failed reads:\n${JSON.stringify(results, null, 2)}`).toEqual([]);
  });
});
