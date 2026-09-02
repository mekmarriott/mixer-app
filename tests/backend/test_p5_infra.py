"""Startup precompute, bounded DB concurrency, and the zero-state deck.

New requirements, so a new ID series (INF-xx). They are mapped in
docs/automation-test-manifest.md alongside the testing-document items.

INF-01  waveform envelopes are precomputed at startup and served without DB reads
INF-02  DB concurrency is bounded, and a connection is never shared concurrently
INF-03  concurrent catalog reads all succeed (the API-01 regression guard)
INF-04  warmup reports progress, and catalog endpoints are gated until ready
INF-05  the zero state browses by genre with a bounded count, no pair analysis
INF-06  popularity orders the deck when present, deterministically otherwise
"""
import concurrent.futures as cf
import threading
import time
import unittest

from fixture import get_fixture, read

from backend import config, dbguard, deck, waveforms
from backend.app import create_app


class TestInfraApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database, _, cls.tmp = get_fixture()
        cls.app = create_app(run_ingestion=False, database=cls.database)
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        cls.guard = cls.app.config["DATABASE"]
        cls.cache = cls.app.config["WAVEFORMS"]
        cls.warm = cls.app.config["WARMUP"]

    @staticmethod
    def _track_ids():
        with read() as q:
            return [t.id for t in q.list_track_summaries()]

    # ------------------------------------------------------------- INF-01
    def test_inf_01_waveforms_precomputed_at_startup(self):
        """Every track has both UI envelopes warm before the server is ready —
        the deck never computes one on demand."""
        # Only tracks with analysis have an envelope to precompute. Unmixable
        # tracks are refused at the licence gate before download (LIC-01), so
        # they have no audio, no analysis and nothing to draw — and the deck
        # never offers them either.
        with read() as q:
            ids = [t.id for t in q.list_track_summaries() if t.mixable]
        self.assertTrue(ids)
        for tid in ids:
            for pts in (config.DECK_WAVEFORM_POINTS, config.TIMELINE_WAVEFORM_POINTS):
                self.assertIsNotNone(
                    self.cache.get(tid, pts),
                    f"track {tid} @ {pts} points was not precomputed")

    def test_inf_01_deck_request_reads_no_database(self):
        """The opening deck is served entirely from the warmup snapshot."""
        before = self.guard.snapshot()["admitted"]
        r = self.client.get("/api/deck")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.guard.snapshot()["admitted"], before,
                         "/api/deck touched the database")

    def test_inf_01_deck_inlines_waveforms(self):
        """Waveforms ship with the deck payload, so a row costs no request."""
        groups = self.client.get("/api/deck").get_json()["groups"]
        rows = [t for g in groups for t in g["tracks"]]
        self.assertTrue(rows)
        for t in rows:
            self.assertIsInstance(t["waveform"], list)
            self.assertEqual(len(t["waveform"]), config.DECK_WAVEFORM_POINTS)

    def test_inf_01_cached_envelope_matches_direct_computation(self):
        """The cache is a cache, not a different code path."""
        tid = self._track_ids()[0]
        with read() as q:
            direct = waveforms.envelope(q.get_track_analysis(id=tid), 64, None)
        served = self.client.get(f"/api/tracks/{tid}/waveform?points=64").get_json()
        self.assertEqual(served["points"], direct["points"])
        self.assertAlmostEqual(served["duration_s"], direct["duration_s"], places=6)

    def test_inf_01_recommendations_inline_candidate_waveforms(self):
        recs = self.client.get("/api/tracks/1001/recommendations").get_json()
        self.assertTrue(recs)
        for r in recs:
            self.assertIn("track", r)
            self.assertEqual(len(r["track"]["waveform"]), config.DECK_WAVEFORM_POINTS)

    # ------------------------------------------------------------- INF-03
    def test_inf_03_concurrent_reads_all_succeed(self):
        """API-01 regression guard at the API layer: the shared-connection race
        produced 500s and phantom 404s here."""
        ids = self._track_ids()

        def hit(tid):
            return self.client.get(f"/api/tracks/{tid}").status_code

        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            codes = list(ex.map(hit, ids * 8))
        self.assertEqual(set(codes), {200}, f"non-200 responses: {sorted(set(codes))}")

    # ------------------------------------------------------------- INF-04
    def test_inf_04_status_reports_ready_with_pool_bounds(self):
        s = self.client.get("/api/status").get_json()
        self.assertTrue(s["ready"])
        self.assertEqual(s["phase"], "ready")
        self.assertEqual(s["percent"], 100)
        self.assertLessEqual(s["db"]["peak_in_flight"], s["db"]["max_concurrency"])

    def test_inf_04_health_is_never_gated(self):
        """Liveness must answer even mid-warmup, or nothing can poll it."""
        self.warm._set(ready=False, phase="ingesting")
        try:
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertEqual(self.client.get("/api/status").status_code, 200)
        finally:
            self.warm._set(ready=True, phase="ready")

    def test_inf_04_catalog_endpoints_gated_until_ready(self):
        """A browser arriving mid-warmup gets 503 + Retry-After and a progress
        payload it can render, not a partial page."""
        before = self.warm.snapshot()
        self.warm._set(ready=False, phase="ingesting", done=2, total=9,
                       message="Analyzing")
        try:
            for path in ("/api/deck", "/api/tracks", "/api/tracks/1001/waveform"):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 503, path)
                self.assertEqual(r.headers.get("Retry-After"), "1", path)
                body = r.get_json()
                self.assertEqual(body["error"], "warming_up")
                self.assertEqual(body["status"]["done"], 2)
                self.assertEqual(body["status"]["total"], 9)
        finally:
            self.warm._set(ready=True, phase="ready", done=before["done"],
                           total=before["total"], message=before["message"])

    def test_inf_04_failed_warmup_reports_500_not_503(self):
        """A failed startup is not 'try again shortly' — it needs attention."""
        self.warm._set(ready=False, phase="failed", error="boom")
        try:
            r = self.client.get("/api/deck")
            self.assertEqual(r.status_code, 500)
            self.assertEqual(r.get_json()["status"]["error"], "boom")
        finally:
            self.warm._set(ready=True, phase="ready", error=None)

    # ------------------------------------------------------------- INF-05
    def test_inf_05_deck_groups_by_genre_within_cap(self):
        payload = self.client.get("/api/deck").get_json()
        self.assertEqual(payload["per_genre"], config.DECK_TRACKS_PER_GENRE)
        self.assertTrue(payload["groups"])
        for g in payload["groups"]:
            self.assertLessEqual(len(g["tracks"]), config.DECK_TRACKS_PER_GENRE)
            self.assertEqual(g["showing"], len(g["tracks"]))
            self.assertTrue(all(t["genre"] == g["genre"] for t in g["tracks"]))

    def test_inf_05_zero_state_carries_no_scores(self):
        """Nothing is selected, so nothing can be ranked — the deck must not
        invent a score."""
        groups = self.client.get("/api/deck").get_json()["groups"]
        for t in (t for g in groups for t in g["tracks"]):
            self.assertNotIn("score", t)
            self.assertNotIn("breakdown", t)


class TestAdmissionControl(unittest.TestCase):
    """INF-02 — the coordinator itself, independent of Flask.

    backend/db already guarantees a connection is never shared between threads.
    What this covers is the other half: an explicit ceiling on how many
    requests are inside the database at once, which per-thread connections do
    not provide (SQLite mints one per thread, and nothing caps the threads).
    """

    def setUp(self):
        database, _, _ = get_fixture()
        self.guard = dbguard.BoundedDatabase(database, max_concurrency=2)

    def test_inf_02_concurrency_must_stay_below_connection_ceiling(self):
        """When the engine advertises a ceiling, admission must sit strictly
        below it so an admitted caller never queues again downstream."""
        database, _, _ = get_fixture()

        class FakePool:
            max_size = 4

        class FakeEngine:
            _pool = FakePool()

        class FakeDB:
            engine = FakeEngine()

        self.assertEqual(dbguard.connection_ceiling(FakeDB()), 4)
        with self.assertRaises(ValueError):
            dbguard.BoundedDatabase(FakeDB(), max_concurrency=4)
        with self.assertRaises(ValueError):
            dbguard.BoundedDatabase(FakeDB(), max_concurrency=9)
        dbguard.BoundedDatabase(FakeDB(), max_concurrency=3)   # below: fine

        # What the real engine advertises depends on which one it is, and the
        # suite runs against both (`make test-pg`). SQLite makes a connection
        # per thread and has no intrinsic ceiling, so admission itself is the
        # bound; Postgres reports its pool size and admission must sit strictly
        # below it.
        ceiling = dbguard.connection_ceiling(database)
        if database.dialect == "sqlite":
            self.assertIsNone(ceiling)
        else:
            self.assertEqual(ceiling, database.engine._pool.max_size)
            self.assertLess(dbguard.BoundedDatabase(database).max_concurrency,
                            ceiling)

    def test_inf_02_semaphore_caps_in_flight_work(self):
        peak = {"n": 0}
        current = {"n": 0}
        lock = threading.Lock()

        def worker(_):
            with self.guard.reading() as q:
                with lock:
                    current["n"] += 1
                    peak["n"] = max(peak["n"], current["n"])
                q.count_tracks()
                time.sleep(0.02)
                with lock:
                    current["n"] -= 1

        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(worker, range(32)))

        self.assertLessEqual(peak["n"], self.guard.max_concurrency)
        self.assertLessEqual(self.guard.snapshot()["peak_in_flight"],
                             self.guard.max_concurrency)

    def test_inf_02_nested_scopes_do_not_deadlock(self):
        """Read/write scopes nest — an inner scope joins the outer transaction.
        The gate must be re-entrant per thread, or a saturated limit would
        deadlock a thread against a permit it already holds."""
        self.guard.max_concurrency  # limit is 2; this nests 3 deep
        with self.guard.reading() as outer:
            self.assertIsNotNone(outer.count_tracks())
            with self.guard.reading() as mid:
                self.assertIsNotNone(mid.count_tracks())
                with self.guard.reading() as inner:
                    self.assertIsNotNone(inner.count_tracks())
        self.assertEqual(self.guard.snapshot()["in_flight"], 0)

    def test_inf_02_permit_released_after_an_error(self):
        """A raising caller must not leak its permit, or the gate bleeds out."""
        for _ in range(10):
            with self.assertRaises(ValueError):
                with self.guard.reading() as q:
                    q.count_tracks()
                    raise ValueError("boom")
        self.assertEqual(self.guard.snapshot()["in_flight"], 0)
        with self.guard.reading() as q:       # still usable
            self.assertIsNotNone(q.count_tracks())


class TestZeroStateSelection(unittest.TestCase):
    """INF-05 / INF-06 — selection logic, independent of the DB."""

    def _tracks(self, n, genre, **extra):
        return [{"id": f"{genre}{i}", "genre": genre, "mixable": True, **extra}
                for i in range(n)]

    def test_inf_05_caps_tracks_per_genre(self):
        tracks = self._tracks(12, "house") + self._tracks(3, "downtempo")
        groups = deck.genre_groups(tracks, per_genre=5)
        by_genre = {g["genre"]: g for g in groups}
        self.assertEqual(len(by_genre["house"]["tracks"]), 5)
        self.assertEqual(by_genre["house"]["total"], 12)
        self.assertEqual(len(by_genre["downtempo"]["tracks"]), 3)

    def test_inf_05_largest_genre_leads(self):
        groups = deck.genre_groups(
            self._tracks(2, "downtempo") + self._tracks(9, "house"), per_genre=5)
        self.assertEqual([g["genre"] for g in groups], ["house", "downtempo"])

    def test_inf_06_popularity_orders_when_present(self):
        tracks = [{"id": str(i), "genre": "house", "popularity": i}
                  for i in range(10)]
        picked = deck.genre_groups(tracks, per_genre=3)[0]["tracks"]
        self.assertEqual([t["id"] for t in picked], ["9", "8", "7"])
        self.assertTrue(deck.genre_groups(tracks, per_genre=3)[0]["has_popularity"])

    def test_inf_06_falls_back_to_a_deterministic_shuffle(self):
        """No popularity today (nothing stores it yet), so selection must at
        least be stable across restarts rather than arbitrary per request."""
        tracks = self._tracks(20, "house")
        first = deck.genre_groups(tracks, per_genre=5)[0]["tracks"]
        again = deck.genre_groups(list(reversed(tracks)), per_genre=5)[0]["tracks"]
        self.assertEqual([t["id"] for t in first], [t["id"] for t in again])
        self.assertFalse(deck.genre_groups(tracks, per_genre=5)[0]["has_popularity"])
        # ...and not merely the input order.
        self.assertNotEqual([t["id"] for t in first], [t["id"] for t in tracks[:5]])

    def test_inf_06_partial_popularity_ranks_known_values_first(self):
        tracks = ([{"id": "hot", "genre": "house", "popularity": 99}]
                  + self._tracks(5, "house"))
        picked = deck.genre_groups(tracks, per_genre=3)[0]["tracks"]
        self.assertEqual(picked[0]["id"], "hot")

    def test_inf_05_unmixable_tracks_are_hidden_not_greyed(self):
        """A track the user cannot use does not belong in the deck at all.

        An ND licence forbids the time-stretch that mixing requires, so no
        variants are ever rendered and a drag would be refused. Showing the
        row disabled costs a deck slot and reads as a fault rather than a
        licence term. Compliance is unaffected: attribution is owed wherever a
        track is played or listed, and an omitted track is neither.
        """
        tracks = self._tracks(2, "house") + [
            {"id": "nd", "genre": "house", "mixable": False}]
        group = deck.genre_groups(tracks, per_genre=5)[0]
        self.assertNotIn("nd", [t["id"] for t in group["tracks"]])
        # The count reflects what is on offer, not what was filtered away.
        self.assertEqual(group["total"], 2)

    def test_inf_05_unfinished_ingestion_is_hidden(self):
        """A track still ingesting has no variants yet, so it cannot be mixed."""
        tracks = [{"id": "ready", "genre": "house", "mixable": True, "status": "ready"},
                  {"id": "midway", "genre": "house", "mixable": True,
                   "status": "analyzed"}]
        ids = [t["id"] for t in deck.genre_groups(tracks, per_genre=5)[0]["tracks"]]
        self.assertEqual(ids, ["ready"])

    def test_inf_05_unusable_tracks_can_still_be_requested_explicitly(self):
        """The filter is a deck-presentation choice, not a data restriction."""
        tracks = self._tracks(1, "house") + [
            {"id": "nd", "genre": "house", "mixable": False}]
        group = deck.genre_groups(tracks, per_genre=5, include_unusable=True)[0]
        self.assertIn("nd", [t["id"] for t in group["tracks"]])


if __name__ == "__main__":
    unittest.main()
