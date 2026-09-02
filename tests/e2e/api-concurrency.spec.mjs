// Regression test for a defect the browser suite found on its first run.
//
// THE DEFECT (fixed)
// backend/app.py opened ONE sqlite3.Connection with check_same_thread=False and
// shared it across every Flask worker thread. The old backend/db.py guarded
// writes with WRITE_LOCK but took no lock on reads. Concurrent execute() calls
// on a single connection interleave, so reads either raised
// (sqlite3.InterfaceError -> HTTP 500) or returned a phantom-empty row that the
// endpoint reported as HTTP 404 for a track that plainly exists — roughly
// 15-20% of concurrent reads, versus 0% sequentially.
//
// THE FIX, in two independent halves
//   * Correctness: backend/db/ hands each thread its own connection, scoped to
//     the request (engine.py: SQLiteEngine, and the reading()/writing()
//     scopes). There is no shared cursor left to interleave.
//   * Bounded load: backend/dbguard.py admits at most DB_MAX_CONCURRENCY
//     requests into the database at once. Per-thread connections alone do not
//     bound anything on SQLite — one connection per thread, and nothing caps
//     the threads.
//
// design-document.md §6 used to claim "check_same_thread=False + a write lock
// handles Flask's threaded server"; it did not, and §6 now describes both
// halves.
//
// This stays a live regression test: it is invisible to the unittest suite,
// which drives Flask's test_client single-threaded.
import { test, expect } from "@playwright/test";
import { bootApp } from "./helpers.mjs";

const BURST = 48;

test.describe("catalog API under concurrent reads", () => {
  test("API-01 parallel catalog reads all succeed", async ({ page }) => {
    await bootApp(page);

    const ids = await page.evaluate(async () =>
      (await (await fetch("/api/tracks")).json()).map((t) => t.id));
    expect(ids.length).toBeGreaterThan(0);

    // Fire the burst from the page so the requests genuinely land on separate
    // Flask worker threads. /api/tracks/<id> is used rather than a waveform
    // read because waveforms are now served from the warmup cache and would
    // never reach the database — this has to exercise the real read path.
    const failures = await page.evaluate(async ({ ids, burst }) => {
      const urls = Array.from({ length: burst },
        (_, i) => `/api/tracks/${ids[i % ids.length]}`);
      const statuses = await Promise.all(
        urls.map((u) => fetch(u).then((r) => ({ url: u, status: r.status }))));
      return statuses.filter((s) => s.status !== 200);
    }, { ids, burst: BURST });

    expect(failures, `failed reads:\n${JSON.stringify(failures, null, 2)}`).toEqual([]);
  });

  test("API-02 admission caps how many requests are inside the database", async ({ page }) => {
    await bootApp(page);

    const ids = await page.evaluate(async () =>
      (await (await fetch("/api/tracks")).json()).map((t) => t.id));

    await page.evaluate(async ({ ids, burst }) => {
      const urls = Array.from({ length: burst },
        (_, i) => `/api/tracks/${ids[i % ids.length]}`);
      await Promise.all(urls.map((u) => fetch(u)));
    }, { ids, burst: BURST });

    const db = await page.evaluate(async () =>
      (await (await fetch("/api/status")).json()).db);

    // The semaphore, not luck, is what held concurrency down.
    expect(db.peak_in_flight).toBeLessThanOrEqual(db.max_concurrency);
    expect(db.timeouts).toBe(0);
    // And the limit stays under the engine's ceiling when it advertises one
    // (Postgres pool max_size; SQLite has none, so admission IS the bound).
    if (db.connection_ceiling !== null) {
      expect(db.max_concurrency).toBeLessThan(db.connection_ceiling);
    }
  });
});
