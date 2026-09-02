"""Phase 5 — deployment infrastructure.

Covers the pieces that only exist because the app is going to Vercel: the blob
storage seam, outbound rate limiting for the track source, and the parallel
batch publisher. All of it runs against the local backend and SQLite, with no
network and no cloud credentials — which is the point. The deployed
configuration differs from this one by two environment variables, so anything
these tests cannot reach is deliberately kept trivial.
"""
import os
import time
import unittest
from unittest import mock

from fixture import get_fixture, read

from backend import config, jamendo, publish, ratelimit, storage
from backend.app import create_app


class TestBlobStore(unittest.TestCase):
    """The local store is the test double for a cloud bucket."""

    def setUp(self):
        _, _, self.tmp = get_fixture()
        self.store = storage.LocalBlobStore(root=self.tmp)

    def test_keys_are_provider_agnostic(self):
        """Keys carry no host, no scheme and no local path — which is what
        makes the Vercel Blob -> R2 move an env var, not a migration."""
        ext = config.delivery_ext()
        self.assertEqual(storage.master_key("1001"), f"audio/1001.{ext}")
        self.assertEqual(storage.variant_key("1001", 120),
                         f"variants/1001_120.{ext}")
        # The PCM master keeps the historical name; only what ships is encoded.
        self.assertEqual(storage.master_source_key("1001"), "audio/1001.wav")
        for key in (storage.master_key("1001"), storage.variant_key("1001", 120)):
            self.assertNotIn("://", key)
            self.assertFalse(key.startswith("/"))

    def test_roundtrip_and_url(self):
        key = "audio/roundtrip.wav"
        self.store.put_bytes(key, b"abc", "audio/wav")
        self.assertTrue(self.store.exists(key))
        self.assertEqual(self.store.local_path(key).read_bytes(), b"abc")
        self.assertEqual(self.store.url_for(key), "/blobs/audio/roundtrip.wav")

    def test_missing_key_not_exists(self):
        self.assertFalse(self.store.exists("audio/nope.wav"))

    def test_key_cannot_escape_store_root(self):
        """Keys are ingest-controlled today, but this store is one refactor
        away from being addressed by a request path."""
        for bad in ("../outside.wav", "audio/../../outside.wav"):
            with self.assertRaises(storage.BlobStoreError):
                self.store.put_bytes(bad, b"x")

    def test_layout_matches_historical_data_dir(self):
        """Moving to key-addressed storage must not have moved any file: the
        existing fixture masters and variants are reachable by key."""
        with read() as q:
            summaries = q.list_track_summaries()
            keys = [(t.id, q.get_track(id=t.id).audio_key)
                    for t in summaries if t.mixable]
        self.assertTrue(keys)
        for tid, key in keys:
            # Unmixable tracks are skipped before download (LIC-01), so they
            # have no master and no key — by design, not by omission.
            self.assertIsNotNone(key, tid)
            self.assertTrue(self.store.exists(key),
                            f"{tid} master missing at {key}")


class TestIntegrationInjectedConfig(unittest.TestCase):
    """A deployment must work from the variables the Vercel integrations
    inject, without a human copying credentials into a second name.

    Supabase injects MIX_DB_POSTGRES_URL; Blob injects BLOB_STORE_ID. Neither
    knows this app's names. Requiring DJMIXER_DATABASE_URL and BLOB_BASE_URL
    on top meant the deployed function silently fell back to on-disk SQLite on
    a read-only filesystem, and had no URL to redirect audio to.
    """

    def test_database_url_prefers_our_own_variable(self):
        with mock.patch.object(config, "DATABASE_URL", "postgresql://ours/db"), \
                mock.patch.dict(os.environ, {"MIX_DB_POSTGRES_URL": "postgresql://theirs/db"}):
            self.assertEqual(config.database_url(), "postgresql://ours/db")

    def test_database_url_falls_back_to_the_injected_one(self):
        with mock.patch.object(config, "DATABASE_URL", None), \
                mock.patch.dict(os.environ, {"MIX_DB_POSTGRES_URL": "postgresql://pooled/db"}):
            self.assertEqual(config.database_url(), "postgresql://pooled/db")
            self.assertFalse(config.is_local_sqlite())

    def test_pooled_url_wins_over_non_pooling(self):
        """A serverless function must use the transaction pooler."""
        with mock.patch.object(config, "DATABASE_URL", None), \
                mock.patch.dict(os.environ, {
                    "MIX_DB_POSTGRES_URL": "postgresql://pooled/db",
                    "MIX_DB_POSTGRES_URL_NON_POOLING": "postgresql://direct/db"}):
            self.assertEqual(config.database_url(), "postgresql://pooled/db")

    def test_empty_variable_forces_sqlite_and_does_not_fall_through(self):
        """run_tests.sh and CI pass `DJMIXER_DATABASE_URL=` to force SQLite.
        Set-but-empty must mean exactly that: falling through to an injected
        URL would point the whole suite at the production database."""
        with mock.patch.object(config, "DATABASE_URL", ""), \
                mock.patch.dict(os.environ, {"MIX_DB_POSTGRES_URL": "postgresql://prod/db"}):
            self.assertTrue(config.is_local_sqlite())

    def test_non_postgres_injected_value_is_ignored(self):
        """An empty or placeholder value must not be mistaken for a URL —
        `vercel env pull` writes [SENSITIVE] for secrets."""
        with mock.patch.object(config, "DATABASE_URL", None), \
                mock.patch.dict(os.environ, {}, clear=False):
            for var in config.DATABASE_URL_FALLBACK_VARS:
                os.environ.pop(var, None)
            # `vercel env pull` writes this placeholder for secret values.
            os.environ["MIX_DB_POSTGRES_URL"] = "[SENSITIVE]"
            self.assertTrue(config.is_local_sqlite())

    def test_blob_base_url_derived_from_store_id(self):
        derive = storage.VercelBlobStore.base_url_from_store_id
        self.assertEqual(derive("store_9fJ05RBNkmUdVmGn"),
                         "https://9fj05rbnkmudvmgn.public.blob.vercel-storage.com")

    def test_blob_base_url_derivation_rejects_a_non_store_id(self):
        derive = storage.VercelBlobStore.base_url_from_store_id
        for bad in ("", None, "9fj05", "prj_abc"):
            self.assertEqual(derive(bad), "")

    def test_explicit_base_url_still_wins(self):
        with mock.patch.dict(os.environ, {"BLOB_STORE_ID": "store_aaaa"}):
            store = storage.VercelBlobStore(base_url="https://explicit.example.com")
            self.assertEqual(store.url_for("audio/1.wav"),
                             "https://explicit.example.com/audio/1.wav")

    def test_store_id_alone_is_enough_to_serve(self):
        env = {"BLOB_STORE_ID": "store_9fJ05RBNkmUdVmGn"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("BLOB_BASE_URL", None)
            store = storage.VercelBlobStore()
            self.assertEqual(
                store.url_for("variants/1001_120.wav"),
                "https://9fj05rbnkmudvmgn.public.blob.vercel-storage.com"
                "/variants/1001_120.wav")


class TestAudioRedirect(unittest.TestCase):
    """Audio must never be proxied through the API process."""

    @classmethod
    def setUpClass(cls):
        cls.database, _, cls.tmp = get_fixture()
        app = create_app(run_ingestion=False, database=cls.database)
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_master_redirects_to_blob_url(self):
        r = self.client.get("/api/tracks/1001/audio")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"],
                         f"/blobs/audio/1001.{config.delivery_ext()}")

    def test_variant_redirects_to_blob_url(self):
        with read() as q:
            bpm = q.list_variants_for_track(track_id="1001")[0].grid_bpm
        r = self.client.get(f"/api/tracks/1001/audio?bpm={bpm}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.headers["Location"],
            f"/blobs/variants/1001_{bpm}.{config.delivery_ext()}")

    def test_redirect_body_is_empty(self):
        """The whole point: the function returns a header, not the audio. A
        regression that starts streaming bytes again would still 200 after the
        redirect, so assert on the size of the redirect itself."""
        r = self.client.get("/api/tracks/1001/audio")
        self.assertLess(len(r.data), 1024)

    def test_unknown_variant_still_404(self):
        self.assertEqual(
            self.client.get("/api/tracks/1001/audio?bpm=200").status_code, 404)

    def test_blob_route_rejects_traversal(self):
        r = self.client.get("/blobs/../../etc/passwd")
        self.assertIn(r.status_code, (301, 308, 400, 404))


class TestVercelBlobStoreConfig(unittest.TestCase):
    """The remote backend's pure configuration logic.

    No network and no CLI: these cover the branches that decide whether a
    deploy can serve audio at all, which is where an untested failure is most
    expensive — the app would start cleanly and 500 on every audio request.
    """

    def test_access_defaults_to_public(self):
        """Public is not a preference. The 302 design needs the object to be
        anonymously readable."""
        self.assertEqual(storage.VercelBlobStore(access=None,
                                                 base_url="https://x").access,
                         "public")

    def test_invalid_access_is_rejected_at_construction(self):
        with self.assertRaises(storage.BlobStoreError):
            storage.VercelBlobStore(access="publik", base_url="https://x")

    def test_private_store_refuses_to_emit_a_client_url(self):
        """A private blob is not anonymously readable, so redirecting a browser
        to it 404s. Failing loudly beats handing out a broken URL."""
        store = storage.VercelBlobStore(access="private", base_url="https://x")
        with self.assertRaises(storage.BlobStoreError) as ctx:
            store.url_for("audio/1001.wav")
        self.assertIn("private", str(ctx.exception))

    def test_url_for_uses_the_configured_base(self):
        store = storage.VercelBlobStore(base_url="https://cdn.example.com/")
        self.assertEqual(store.url_for("audio/1001.wav"),
                         "https://cdn.example.com/audio/1001.wav")

    def test_missing_base_url_raises_rather_than_guessing(self):
        """api/index.py sets BLOB_BACKEND=vercel, so having no way at all to
        build a URL is a live deploy failure. It must say so, not emit a
        relative path.

        The store id is cleared too: it is now a second source for the base
        URL, so leaving it set would mean this asserted nothing.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLOB_BASE_URL", None)
            os.environ.pop("BLOB_STORE_ID", None)
            store = storage.VercelBlobStore(base_url="")
            with self.assertRaises(storage.BlobStoreError) as ctx:
                store.url_for("audio/1001.wav")
        self.assertIn("BLOB_BASE_URL", str(ctx.exception))

    def _fake_cli(self, stdout="", stderr="", returncode=0):
        """Patch out the subprocess so put_file's output handling is testable."""
        return (mock.patch.object(storage.subprocess, "run",
                                  return_value=mock.Mock(returncode=returncode,
                                                         stdout=stdout, stderr=stderr)),
                mock.patch.object(storage.shutil, "which", return_value="/bin/vercel"))

    def test_success_url_is_read_from_stderr(self):
        """`vercel blob put` writes "Success! <url>" — and everything else — to
        stderr, leaving stdout empty. Verified against the real CLI (59.11.1).
        Scanning stdout alone finds no URL, so every upload that in fact
        succeeded raised "could not parse blob URL from CLI output: ''"."""
        url = "https://s.blob.vercel-storage.com/audio/1001.wav"
        store = storage.VercelBlobStore(access="public", base_url="https://s")
        run, which = self._fake_cli(stderr=f"Uploading blob\n> Success! {url}\n")
        with run, which:
            self.assertEqual(store.put_file("audio/1001.wav", "/tmp/x.wav"),
                             "audio/1001.wav")
        self.assertEqual(store.url_for("audio/1001.wav"), url)

    def test_private_upload_does_not_require_a_url(self):
        """A private store has no anonymously readable URL to report, so
        demanding one would fail an upload that actually succeeded."""
        store = storage.VercelBlobStore(access="private", base_url="https://s")
        run, which = self._fake_cli(stderr="Uploading blob\n")
        with run, which:
            self.assertEqual(store.put_file("audio/1001.wav", "/tmp/x.wav"),
                             "audio/1001.wav")

    def test_public_upload_without_a_url_is_still_an_error(self):
        """The relaxation above must not hide a genuinely missing URL on the
        one access level whose serving path needs it."""
        store = storage.VercelBlobStore(access="public", base_url="https://s")
        run, which = self._fake_cli(stderr="Uploading blob\n")
        with run, which:
            with self.assertRaises(storage.BlobStoreError):
                store.put_file("audio/1001.wav", "/tmp/x.wav")


class TestTokenBucket(unittest.TestCase):
    """Rate limiting is the only thing between a 10k-track run and a flood,
    so it is tested on a fake clock rather than by sleeping."""

    def _bucket(self, rate, burst=None):
        self.now = 0.0
        self.slept = []

        def clock():
            return self.now

        def sleep(d):
            self.slept.append(d)
            self.now += d

        return ratelimit.TokenBucket(rate, burst=burst, clock=clock, sleep=sleep)

    def test_burst_then_throttle(self):
        b = self._bucket(rate=2.0, burst=2)
        b.acquire()
        b.acquire()
        self.assertEqual(self.slept, [])        # burst is free
        b.acquire()
        self.assertAlmostEqual(sum(self.slept), 0.5, places=6)   # 1/rate

    def test_sustained_rate_is_respected(self):
        b = self._bucket(rate=4.0, burst=1)
        for _ in range(9):
            b.acquire()
        # 1 free + 8 paced at 0.25s
        self.assertAlmostEqual(self.now, 2.0, places=6)

    def test_try_acquire_does_not_block(self):
        b = self._bucket(rate=1.0, burst=1)
        self.assertTrue(b.try_acquire())
        self.assertFalse(b.try_acquire())
        self.assertEqual(self.slept, [])

    def test_rate_must_be_positive(self):
        with self.assertRaises(ValueError):
            ratelimit.TokenBucket(0)


class TestRequestBudget(unittest.TestCase):
    def test_spends_and_reports(self):
        b = ratelimit.RequestBudget(3)
        b.spend(2)
        self.assertEqual(b.used, 2)
        self.assertEqual(b.remaining, 1)

    def test_raises_rather_than_overspending(self):
        """A monthly quota is spent by retry loops, not by bursts — so the
        budget aborts instead of throttling."""
        b = ratelimit.RequestBudget(2)
        b.spend(2)
        with self.assertRaises(ratelimit.BudgetExceeded):
            b.spend(1)


class TestBackoff(unittest.TestCase):
    def test_full_jitter_bounds(self):
        delays = ratelimit.backoff_delays(attempts=5, base=1.0, cap=60.0)
        self.assertEqual(len(delays), 5)
        for i, d in enumerate(delays):
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, min(60.0, 1.0 * 2 ** i) + 1e-9)

    def test_jitter_desynchronises_workers(self):
        """Fixed exponential backoff makes parallel workers retry in lockstep,
        which converts a rate limit into a self-inflicted stampede."""
        runs = [ratelimit.backoff_delays(attempts=6) for _ in range(20)]
        self.assertGreater(len({tuple(r) for r in runs}), 1)

    def test_retry_after_header_wins_over_backoff(self):
        self.assertEqual(ratelimit.retry_after_seconds({"Retry-After": "12"}), 12.0)
        self.assertEqual(ratelimit.retry_after_seconds({"retry-after": "3.5"}), 3.5)

    def test_retry_after_absent_or_http_date_falls_back(self):
        self.assertEqual(ratelimit.retry_after_seconds({}, default=7), 7)
        self.assertEqual(
            ratelimit.retry_after_seconds(
                {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, default=7), 7)


class TestJamendoRequestEconomy(unittest.TestCase):
    def test_batching_collapses_request_count(self):
        """Metadata for 10k tracks costs 200 requests batched and 10,000 not —
        against a monthly quota, that difference is the whole ballgame."""
        self.assertEqual(jamendo.estimate_api_requests(10_000), 200)
        self.assertEqual(jamendo.estimate_api_requests(1), 1)
        self.assertEqual(jamendo.estimate_api_requests(50), 1)
        self.assertEqual(jamendo.estimate_api_requests(51), 2)

    def test_id_batch_stays_within_the_multi_value_cap(self):
        """The API rejects a request carrying more than 50 values in one
        parameter, whole. This is NOT the `limit` cap (200) — conflating them
        works for any catalog under 50 tracks and then fails outright on the
        first import large enough to need batching at all."""
        self.assertLessEqual(jamendo.MAX_IDS_PER_REQUEST, 50)
        self.assertEqual(jamendo.MAX_RESULTS_PER_REQUEST, 200)

    def test_no_batch_exceeds_the_cap_for_a_large_catalog(self):
        sent = []

        def get(url, params, timeout=30, limiter=None, budget=None):
            sent.append(params["id"].split())
            return {"headers": {"code": 0},
                    "results": [{"id": i, "name": "n", "artist_name": "a",
                                 "license_ccurl":
                                     "http://creativecommons.org/licenses/by/3.0/",
                                 "audiodownload_allowed": True,
                                 "audiodownload": "http://x/a.mp3",
                                 "shareurl": ""}
                                for i in params["id"].split()]}

        jamendo.fetch_metadata([str(i) for i in range(1200)],
                               client_id="x", get=get, sleep=lambda _s: None)
        self.assertTrue(sent)
        self.assertLessEqual(max(len(b) for b in sent), 50)
        self.assertEqual(sum(len(b) for b in sent), 1200)


class _FakeGet:
    """Replays scripted JSON payloads and records the params it saw.

    Stands in for jamendo._batch_get_json, which is the seam the batch path
    takes its transport from — so budget and limiter behaviour is exercised
    here rather than mocked away.
    """

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, url, params, timeout=30, limiter=None, budget=None):
        if budget is not None:
            budget.spend(1)
        if limiter is not None:
            limiter.acquire()
        self.calls.append(params or {})
        return self.payloads.pop(0)


def _track(tid):
    return {"id": tid, "name": f"n{tid}", "artist_name": "a",
            "license_ccurl": "http://creativecommons.org/licenses/by/4.0/",
            "audiodownload_allowed": True, "audiodownload": "http://x/y.mp3"}


class TestJamendoEmptyResultRetry(unittest.TestCase):
    """The API's dominant failure mode returns HTTP 200 with an empty result
    set and error code 0 — invisible to status- and header-based checks.
    Observed loss is all-or-nothing, so a short batch means 'retry', never
    'those tracks are gone'. Accepting a short batch would silently drop
    tracks from a 10k import while reporting success.
    """

    def test_retries_until_complete(self):
        ids = ["1", "2", "3"]
        get = _FakeGet([
            {"results": []},
            {"results": []},
            {"results": [_track(i) for i in ids]},
        ])
        got = jamendo.fetch_metadata(ids, get=get, client_id="cid",
                                     sleep=lambda d: None)
        self.assertEqual(sorted(got), ids)
        self.assertEqual(len(get.calls), 3)

    def test_short_batch_raises_rather_than_silently_dropping(self):
        ids = ["1", "2", "3"]
        get = _FakeGet([{"results": [_track("1")]}]
                       * (jamendo.EMPTY_RESULT_RETRIES + 1))
        with self.assertRaises(jamendo.IncompleteBatch):
            jamendo.fetch_metadata(ids, get=get, client_id="cid",
                                   sleep=lambda d: None)

    def test_ids_are_space_joined_for_plus_encoding(self):
        """Repeating `id=` does not batch — the API keeps the last value and
        returns one row. The ids must reach the wire as `id=a+b+c`, which is
        what urlencoding a space-joined string produces; a literal '+' would
        be escaped to %2B and break batching silently."""
        ids = ["11", "22", "33"]
        get = _FakeGet([{"results": [_track(i) for i in ids]}])
        jamendo.fetch_metadata(ids, get=get, client_id="cid",
                               sleep=lambda d: None)
        self.assertEqual(get.calls[0]["id"], "11 22 33")
        self.assertNotIn("+", get.calls[0]["id"])

    def test_budget_is_spent_per_attempt_including_failures(self):
        """A failed attempt was still a real request. Budgeting only successes
        is how a retry loop quietly eats a monthly quota."""
        budget = ratelimit.RequestBudget(10)
        get = _FakeGet([{"results": []}, {"results": [_track("1")]}])
        jamendo.fetch_metadata(["1"], get=get, client_id="cid",
                               budget=budget, sleep=lambda d: None)
        self.assertEqual(budget.used, 2)

    def test_budget_exhaustion_aborts_before_the_network(self):
        budget = ratelimit.RequestBudget(1)
        budget.spend(1)
        get = _FakeGet([{"results": [_track("1")]}])
        with self.assertRaises(ratelimit.BudgetExceeded):
            jamendo.fetch_metadata(["1"], get=get, client_id="cid",
                                   budget=budget, sleep=lambda d: None)
        self.assertEqual(get.calls, [])


class TestPublisherPlanning(unittest.TestCase):
    """Resumability: a 10k import will be interrupted, and re-running must be
    cheap and correct rather than a restart."""

    def setUp(self):
        self.database, _, self.tmp = get_fixture()
        self.store = storage.LocalBlobStore(root=self.tmp)
        with read() as q:
            self.entries = [{"id": t.id, "genre": t.genre}
                            for t in q.list_track_summaries()]

    def test_fully_published_tracks_are_skipped(self):
        todo, skipped = publish.plan(self.database, self.store, self.entries)
        self.assertEqual(todo, [])
        self.assertEqual(len(skipped), len(self.entries))

    def test_unknown_track_is_pending(self):
        todo, _ = publish.plan(self.database, self.store, [{"id": "9999"}])
        self.assertEqual(len(todo), 1)

    def test_row_without_its_blob_is_not_done(self):
        """A catalog row whose audio never reached the store is worse than a
        missing row: the API would hand clients a 302 to a URL that 404s."""
        class Missing(storage.LocalBlobStore):
            def exists(self, key):
                return False

        with read() as q:
            unmixable = {t.id for t in q.list_track_summaries() if not t.mixable}

        todo, skipped = publish.plan(self.database, Missing(root=self.tmp), self.entries)

        # Every track that HAS audio is queued again, because none of it is
        # reachable. Unmixable tracks are out of scope: they were refused at
        # the licence gate and have no blob to be missing.
        self.assertEqual({e["id"] for e in skipped}, unmixable)
        self.assertEqual(len(todo), len(self.entries) - len(unmixable))
        self.assertTrue(todo)

    def test_nd_track_is_done_without_variants(self):
        """1005 is CC BY-ND: refused at the licence gate before download, so it
        has neither variants NOR a master. Planning has to treat it as done, or
        every run would queue it again and the skip would save nothing."""
        nd = [e for e in self.entries if e["id"] == "1005"]
        self.assertTrue(nd)
        _, skipped = publish.plan(self.database, self.store, nd)
        self.assertEqual(len(skipped), 1)

    def test_offline_mode_costs_no_api_requests(self):
        cost = publish.budget_report(self.entries, "offline")
        self.assertEqual(cost["metadata_requests"], 0)
        self.assertEqual(cost["downloads"], 0)

    def test_jamendo_cost_is_reported_before_spending(self):
        cost = publish.budget_report([{"id": i} for i in range(500)], "jamendo")
        self.assertEqual(cost["metadata_requests"], 10)   # 500 / 50 per batch
        self.assertEqual(cost["downloads"], 500)


class TestStaleSchemaGuard(unittest.TestCase):
    """migrate() only creates, never alters, and rows map to dataclasses
    positionally — so a database built before a column rename returns the old
    column's value under the new field name, and nothing raises anywhere.
    Every endpoint answers 200 and the only symptom is wrong data at the far
    end. verify_schema() converts that into a startup error."""

    def _stale_db(self, tmpdir):
        import sqlite3
        path = tmpdir / "stale.sqlite3"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, artist TEXT,
              genre TEXT, license TEXT, license_nd INTEGER, license_sa INTEGER,
              license_nc INTEGER, mixable INTEGER, native_bpm REAL,
              camelot TEXT, duration_s REAL, audio_path TEXT,
              analysis_json TEXT, segments_json TEXT);
        """)
        con.commit()
        con.close()
        return path

    def test_pre_rename_database_is_rejected(self):
        import tempfile
        from pathlib import Path
        from backend.db import Database, DatabaseError
        with tempfile.TemporaryDirectory() as td:
            path = self._stale_db(Path(td))
            db = Database.from_url(f"sqlite:///{path}")
            try:
                with self.assertRaises(DatabaseError) as ctx:
                    db.migrate()
                msg = str(ctx.exception)
                self.assertIn("audio_path", msg)     # what the database has
                self.assertIn("audio_key", msg)      # what the code expects
                self.assertIn("Delete", msg)         # what to do about it
            finally:
                db.dispose()

    def test_current_schema_passes(self):
        database, _, _ = get_fixture()
        database.verify_schema()        # must not raise


class TestBpmGridNarrowing(unittest.TestCase):
    """The grid spacing is a storage knob; these lock in the properties that
    make narrowing it safe (docs/infrastructure-plan.md §4.3)."""

    def test_spacing_is_five(self):
        for bucket in config.BPM_BUCKETS.values():
            steps = {b - a for a, b in zip(bucket, bucket[1:])}
            self.assertTrue(steps <= {config.GRID_SPACING}, bucket)

    #: Pairs a 1 BPM grid matches that the shipped spacing does not, per band.
    #: Measured, not assumed — see the docstring below. Tightening the spacing
    #: may lower these; raising one means compatibility was traded away and
    #: should be a deliberate decision rather than a silent regression.
    MAX_PAIR_LOSS_PCT = 3.0
    MIN_ABSOLUTE_COVERAGE_PCT = 96.0

    @staticmethod
    def _coverage(natives, grid):
        cap = config.MAX_STRETCH_RATIO

        def pts(n):
            return {g for g in grid if abs(g / n - 1.0) <= cap}

        ok = total = 0
        for i, a in enumerate(natives):
            for b in natives[i + 1:]:
                total += 1
                if pts(a) & pts(b):
                    ok += 1
        return ok, total

    def test_coarsening_costs_a_bounded_amount_of_compatibility(self):
        """How much compatibility the coarse grid actually costs.

        An earlier version of this test asserted the cost was *zero*. That held
        for the two narrow bands the grid originally had, and is false in
        general: in a band wider than roughly a 1.2 max/min ratio, two tracks
        near opposite edges can only meet on an interior point, and a coarse
        grid may not put one where both can reach it. Measured at spacing 5,
        `slow` loses 2.96% of pairs and `midtempo` 2.62%; downtempo, house,
        uptempo and fast lose nothing.

        So the honest claim is bounded, not zero — and worth it, since the
        spacing removes ~75% of stored audio bytes and the same share of
        ingestion compute (docs/infrastructure-plan.md §4.3).
        """
        for band, (lo, hi) in config.BPM_BANDS.items():
            natives = [lo + 0.5 * i for i in range(int((hi - lo) * 2) + 1)]
            fine_ok, total = self._coverage(natives, list(range(lo, hi + 1)))
            coarse_ok, _ = self._coverage(natives, config.BPM_BUCKETS[band])

            lost = 100.0 * (fine_ok - coarse_ok) / fine_ok if fine_ok else 0.0
            self.assertLessEqual(
                lost, self.MAX_PAIR_LOSS_PCT,
                f"{band}: coarsening to {config.GRID_SPACING} BPM loses "
                f"{lost:.2f}% of the pairs a 1 BPM grid matches")
            self.assertGreaterEqual(
                100.0 * coarse_ok / total, self.MIN_ABSOLUTE_COVERAGE_PCT,
                f"{band}: only {100.0 * coarse_ok / total:.1f}% of pairs match")

    def test_the_grid_is_actually_coarser(self):
        """Guards the premise of the test above: if the spacing silently went
        back to 1, every bound here would pass while saving nothing."""
        for band, (lo, hi) in config.BPM_BANDS.items():
            self.assertLess(len(config.BPM_BUCKETS[band]), len(range(lo, hi + 1)),
                            band)

    def test_worst_case_stretch_stays_within_the_cap(self):
        """Coarsening costs stretch, so bound what it costs."""
        from backend import bpm_grid
        for band in config.BPM_BANDS:
            lo, hi = config.BPM_BANDS[band]
            for i in range(int((hi - lo) * 2) + 1):
                native = lo + 0.5 * i
                for g in bpm_grid.grid_points(native, band):
                    self.assertLessEqual(abs(g / native - 1.0),
                                         config.MAX_STRETCH_RATIO + 1e-9)

    def test_genres_remain_separable(self):
        """The 409 'no shared grid' path depends on cross-genre pairs never
        matching; a coarser grid must not accidentally merge two buckets."""
        from backend import bpm_grid
        house = bpm_grid.grid_points(124, "house")
        down = bpm_grid.grid_points(90, "downtempo")
        self.assertEqual(bpm_grid.shared_grid(house, down), [])


if __name__ == "__main__":
    unittest.main()
