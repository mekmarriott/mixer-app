// Regression test for a defect the browser suite found on its first run.
//
// THE DEFECT (fixed)
// backend/app.py opened ONE sqlite3.Connection with check_same_thread=False and
// shared it across every Flask worker thread. The old backend/db.py guarded
// writes with WRITE_LOCK but took no lock on reads (get_track / analysis_of /
// variants_for). Concurrent execute() calls on a single connection interleave,
// so reads either raised (sqlite3.InterfaceError -> HTTP 500) or returned a
// phantom-empty row that the endpoint reported as HTTP 404 for a track that
// plainly exists — roughly 15-20% of concurrent reads, versus 0% sequentially.
//
// THE FIX
// backend/db/ hands each thread its own connection and scopes it to the
// request (see engine.py: SQLiteEngine, and the reading()/writing() scopes).
// There is no shared cursor left to interleave. design-document.md §6 used to
// claim "check_same_thread=False + a write lock handles Flask's threaded
// server"; it did not, and §6 now describes the per-thread arrangement.
//
// This stays as a live regression test: it is invisible to the unittest suite,
// which drives Flask's test_client single-threaded, and only bites when a real
// browser opens the page and the deck fires one waveform request per row in
// parallel.
import { test, expect } from "@playwright/test";
import { bootApp } from "./helpers.mjs";

const BURST = 36;

test.describe("catalog API under concurrent reads", () => {
  test("API-01 parallel catalog reads all succeed (per-thread SQLite connections)", async ({ page }) => {
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
