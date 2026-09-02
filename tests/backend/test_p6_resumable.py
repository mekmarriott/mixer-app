"""Resumable, idempotent ingestion + per-track state.

The rule these guard: work that is already durably on disk is never redone,
and a network fetch is never repeated for a track whose master is persisted.
A crash partway through ingestion must cost only the unfinished work.

These run in `offline` mode so the "did it hit the network" question is
answered by spying on the provider seam rather than by making real requests.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixture import FIXTURE_TRACKS

from backend import config, ingest, jamendo, storage
from backend.db import Database, status
from backend.timing import Timer

import teststore


class ResumableCase(unittest.TestCase):
    """Each test gets its own catalog dir so state changes cannot leak."""

    def setUp(self):
        self._saved = (config.DATA_DIR, config.AUDIO_DIR,
                       config.VARIANT_DIR, config.DB_PATH, config.DATABASE_URL)
        # Overriding the paths is not enough: from_config() also reads
        # DATABASE_URL, which .env.local sets to the shared local PostgreSQL.
        # teststore.isolate() overrides that too. See tests/backend/teststore.py.
        self.tmp = teststore.isolate(
            config, tempfile.mkdtemp(prefix="djresume_"))
        self.database = Database.from_config().migrate()
        # One short house track keeps rendering cheap.
        self.entry = dict(FIXTURE_TRACKS[0], duration_s=8)
        self.tid = self.entry["id"]

    def tearDown(self):
        self.database.dispose()
        (config.DATA_DIR, config.AUDIO_DIR,
         config.VARIANT_DIR, config.DB_PATH, config.DATABASE_URL) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ingest(self, **kw):
        return ingest.ingest_track(self.database, self.entry, "offline",
                                   Timer(self.database), **kw)

    def track(self):
        with self.database.reading() as q:
            return q.get_track(id=self.tid)

    def counting_fetch(self):
        """Patch the provider seam with a counting passthrough."""
        real = jamendo.fetch_track
        calls = []

        def spy(entry, mode):
            calls.append(entry["id"])
            return real(entry, mode)
        return mock.patch.object(jamendo, "fetch_track", spy), calls


class TestStatusProgression(ResumableCase):
    def test_status_reaches_ready(self):
        r = self.ingest()
        self.assertEqual(r["status"], status.READY)
        self.assertEqual(self.database.catalog.status_of(self.tid), status.READY)

    def test_stage_timestamps_recorded(self):
        self.ingest()
        row = self.track()
        for col in ("fetched_at", "analyzed_at", "ready_at"):
            self.assertIsNotNone(getattr(row, col), col)
        self.assertLessEqual(row.fetched_at, row.analyzed_at)
        self.assertLessEqual(row.analyzed_at, row.ready_at)

    def test_unknown_track_is_pending(self):
        self.assertEqual(self.database.catalog.status_of("does-not-exist"),
                         status.PENDING)

    def test_status_ordering(self):
        self.assertTrue(status.at_least(status.READY, status.FETCHED))
        self.assertTrue(status.at_least(status.ANALYZED, status.ANALYZED))
        self.assertFalse(status.at_least(status.FETCHED, status.ANALYZED))
        self.assertFalse(status.at_least(status.PENDING, status.FETCHED))


class TestNoRedundantWork(ResumableCase):
    def test_second_ingest_does_not_refetch(self):
        self.ingest()
        patcher, calls = self.counting_fetch()
        with patcher:
            r = self.ingest()
        self.assertEqual(calls, [], "re-fetched a track already persisted")
        self.assertIn("fetch", r["reused"])
        self.assertEqual(r["status"], status.READY)

    def test_ingest_all_skips_ready_tracks_entirely(self):
        cfg = {"mode": "offline", "tracks": [self.entry]}
        with mock.patch.object(config, "load_tracks_config", return_value=cfg):
            ingest.ingest_all(self.database, Timer(self.database))
            patcher, calls = self.counting_fetch()
            with patcher:
                results = ingest.ingest_all(self.database, Timer(self.database))
        self.assertEqual(calls, [])
        self.assertEqual(results[0]["reused"], ["all"])

    def test_variants_are_not_rerendered(self):
        self.ingest()
        before = {p.name: p.stat().st_mtime_ns
                  for p in (self.tmp / "variants").glob("*.wav")}
        self.assertTrue(before)
        self.ingest()
        after = {p.name: p.stat().st_mtime_ns
                 for p in (self.tmp / "variants").glob("*.wav")}
        self.assertEqual(before, after, "variant files were rewritten")

    def test_force_redoes_everything(self):
        self.ingest()
        patcher, calls = self.counting_fetch()
        with patcher:
            r = self.ingest(force=True)
        self.assertEqual(calls, [self.tid])
        self.assertEqual(r["reused"], [])


class TestCrashRecovery(ResumableCase):
    def _rewind_to(self, new_status, clear_analysis=False):
        """Simulate a crash that left the track at an earlier stage."""
        with self.database.writing() as q:
            q.set_track_status(id=self.tid, status=new_status)
            if clear_analysis:
                q.clear_track_analysis(id=self.tid)

    def test_resumes_from_persisted_master_after_analysis_loss(self):
        """Crash between fetch and analysis: the master on disk must be
        reused, not downloaded again."""
        self.ingest()
        self._rewind_to(status.FETCHED, clear_analysis=True)

        patcher, calls = self.counting_fetch()
        with patcher:
            r = self.ingest()
        self.assertEqual(calls, [], "re-fetched instead of reusing the master")
        self.assertIn("fetch", r["reused"])
        self.assertIsNotNone(self.track().analysis_json)
        self.assertEqual(self.database.catalog.status_of(self.tid), status.READY)

    def test_missing_master_file_forces_refetch(self):
        """State claims fetched but the file is gone — trust the disk."""
        self.ingest()
        Path(storage.get_store().local_path(self.track().audio_key)).unlink()
        patcher, calls = self.counting_fetch()
        with patcher:
            self.ingest()
        self.assertEqual(calls, [self.tid], "did not re-fetch a missing master")

    def test_only_missing_variants_are_rendered(self):
        self.ingest()
        variants = sorted((self.tmp / "variants").glob("*.wav"))
        self.assertGreater(len(variants), 1)
        victim = variants[0]
        victim.unlink()
        self._rewind_to(status.ANALYZED)

        survivors = {p.name: p.stat().st_mtime_ns
                     for p in (self.tmp / "variants").glob("*.wav")}
        self.ingest()
        self.assertTrue(victim.exists(), "missing variant was not restored")
        after = {p.name: p.stat().st_mtime_ns
                 for p in (self.tmp / "variants").glob("*.wav")
                 if p.name in survivors}
        self.assertEqual(survivors, after, "intact variants were re-rendered")

    def test_failure_is_recorded_without_rewinding_progress(self):
        """A crash during analysis records the error but must KEEP the
        `fetched` high-water mark — otherwise the retry re-downloads audio
        that is already sitting on disk."""
        boom = mock.patch("backend.ingest.analysis_mod.analyze",
                          side_effect=RuntimeError("analysis exploded"))
        with boom, self.assertRaises(RuntimeError):
            self.ingest()
        row = self.track()
        self.assertEqual(row.status, status.FETCHED)
        self.assertTrue(status.is_failed(row))
        self.assertIn("analysis exploded", row.status_error)
        self.assertTrue(Path(storage.get_store().local_path(row.audio_key)).exists(),
                        "master was discarded on failure")

        # The retry reuses that master and clears the error.
        patcher, calls = self.counting_fetch()
        with patcher:
            self.ingest()
        self.assertEqual(calls, [])
        self.assertEqual(self.database.catalog.status_of(self.tid), status.READY)
        self.assertIsNone(self.track().status_error)

    def test_one_failure_does_not_abort_the_catalog(self):
        good = dict(FIXTURE_TRACKS[1], duration_s=8)
        cfg = {"mode": "offline",
               "tracks": [dict(self.entry, license="NOT A LICENSE"), good]}
        with mock.patch.object(config, "load_tracks_config", return_value=cfg):
            results = ingest.ingest_all(self.database, Timer(self.database))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(ingest.failed(results)), 1)
        ok = [r for r in results
              if r.get("status") == status.READY and not r.get("error")]
        self.assertEqual([r["id"] for r in ok], [good["id"]])


class TestCatalogIsNotTheConfigFile(ResumableCase):
    """config/tracks.json seeds a local run; it does not define the catalog.

    Tracks published straight into the production database and blob store —
    never named in the seed file — must be first-class: served by the API and
    visible in the ingestion report. The serving endpoints read only the
    database, so the risk is the report claiming `complete` while silently
    omitting them.
    """

    def _publish_directly(self, track_id="9999"):
        """A row that exists only in the database, as a direct publish would."""
        self.ingest()                       # gives us real analysis to clone
        with self.database.reading() as q:
            seed = q.get_track(id=self.tid)
        self.database.catalog.save_ingested_track({
            "id": track_id, "name": "Uploaded Directly", "artist": "Ops",
            "genre": seed.genre, "license": seed.license,
            "nd": seed.license_nd, "sa": seed.license_sa, "nc": seed.license_nc,
            "mixable": True, "native_bpm": seed.native_bpm,
            "camelot": seed.camelot, "duration_s": seed.duration_s,
            "audio_key": seed.audio_key, "analysis": seed.analysis_json,
            "segments": seed.segments_json, "status": status.READY,
        })
        self.database.catalog.advance_status(track_id, status.READY)
        return track_id

    def test_api_serves_a_track_that_is_not_in_the_config(self):
        from backend.app import create_app
        other = self._publish_directly()
        cfg = {"mode": "offline", "tracks": [self.entry]}
        with mock.patch.object(config, "load_tracks_config", return_value=cfg):
            app = create_app(run_ingestion=False, database=self.database)
            app.config["TESTING"] = True
            client = app.test_client()
            listed = [t["id"] for t in client.get("/api/tracks").get_json()]
            self.assertIn(other, listed, "/api/tracks hid a directly-published track")
            self.assertEqual(client.get(f"/api/tracks/{other}").status_code, 200)

    def test_ingest_report_lists_unconfigured_tracks(self):
        from backend.app import create_app
        other = self._publish_directly()
        cfg = {"mode": "offline", "tracks": [self.entry]}
        with mock.patch.object(config, "load_tracks_config", return_value=cfg):
            app = create_app(run_ingestion=False, database=self.database)
            app.config["TESTING"] = True
            body = app.test_client().get("/api/ingest").get_json()

        by_id = {t["id"]: t for t in body["tracks"]}
        self.assertIn(other, by_id, "/api/ingest omitted a directly-published track")
        self.assertFalse(by_id[other]["in_config"])
        self.assertTrue(by_id[self.tid]["in_config"])
        self.assertEqual(body["configured"], 1)
        self.assertEqual(body["unconfigured"], 1)

    def test_completeness_is_about_the_seed_file_only(self):
        """An unconfigured track is not ingestion's job, so it must neither
        break `complete` nor be counted as something still to do."""
        other = self._publish_directly()
        state = self.database.catalog.ingestion_state([self.tid])
        by_id = {s["id"]: s for s in state}
        self.assertEqual(set(by_id), {self.tid, other})
        self.assertTrue(all(s["status"] == status.READY for s in state))

    def test_no_config_means_every_row_counts(self):
        other = self._publish_directly()
        state = self.database.catalog.ingestion_state()
        self.assertEqual({s["id"] for s in state}, {self.tid, other})
        self.assertTrue(all(s["in_config"] for s in state))


class TestIngestStateReporting(ResumableCase):
    def test_reports_configured_but_unstarted_tracks(self):
        state = self.database.catalog.ingestion_state([self.tid, "9999"])
        by_id = {s["id"]: s for s in state}
        self.assertEqual(by_id[self.tid]["status"], status.PENDING)
        self.assertEqual(by_id["9999"]["status"], status.PENDING)

    def test_reports_ready_with_variant_count(self):
        self.ingest()
        state = self.database.catalog.ingestion_state([self.tid])[0]
        self.assertEqual(state["status"], status.READY)
        self.assertGreater(state["variants"], 0)
        self.assertIsNone(state["error"])
        self.assertFalse(state["failed"])

    def test_incremental_upsert_preserves_columns_it_does_not_carry(self):
        """Ingestion writes the row after fetch and again after analysis; the
        earlier write must not blank what the later one stored."""
        self.ingest()
        before = self.track()
        self.database.catalog.save_ingested_track({
            "id": self.tid, "name": before.name, "artist": before.artist,
            "genre": before.genre, "license": before.license,
            "nd": before.license_nd, "sa": before.license_sa,
            "nc": before.license_nc, "mixable": before.mixable,
            "status": status.ANALYZED,
        })
        after = self.track()
        for col in ("name", "artist", "license", "audio_key", "native_bpm",
                    "camelot", "analysis_json", "segments_json", "ready_at"):
            self.assertEqual(getattr(before, col), getattr(after, col), col)
        self.assertEqual(after.status, status.ANALYZED)


class TestSchemaMigration(ResumableCase):
    def test_columns_added_to_an_existing_catalog(self):
        """migrate() must be additive: CREATE TABLE IF NOT EXISTS is a no-op on
        an existing table, so a new column would otherwise never arrive and the
        upgrade would mean re-ingesting."""
        import sqlite3
        path = self.tmp / "legacy.sqlite3"
        con = sqlite3.connect(str(path))
        con.execute("""CREATE TABLE tracks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, artist TEXT NOT NULL,
            genre TEXT NOT NULL, license TEXT NOT NULL,
            license_nd INTEGER NOT NULL, license_sa INTEGER NOT NULL,
            license_nc INTEGER NOT NULL, mixable INTEGER NOT NULL,
            native_bpm REAL, camelot TEXT, duration_s REAL, audio_key     TEXT,
            analysis_json TEXT, segments_json TEXT)""")
        con.execute("INSERT INTO tracks VALUES ('legacy','N','A','house',"
                    "'CC BY 4.0',0,0,0,1,124.0,'8A',60.0,'/tmp/x.wav',NULL,NULL)")
        con.commit()
        con.close()

        Database.from_url(f"sqlite:///{path}").migrate().dispose()

        con = sqlite3.connect(str(path))
        cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
        con.close()
        self.assertTrue({"status", "status_error", "source_url",
                         "fetched_at", "analyzed_at", "ready_at"} <= cols)

    def test_not_null_column_on_a_populated_table_explains_itself(self):
        """A NOT NULL column with no DEFAULT cannot be added to a table that
        already has rows — there is no value for them. That is what a renamed
        column looks like to an additive migration, and the driver's message
        ("Cannot add a NOT NULL column with default value NULL") says nothing
        about which column or why. Fail with something actionable instead."""
        import sqlite3
        from backend.db import DatabaseError
        from backend.db import engine as engine_mod

        path = self.tmp / "renamed.sqlite3"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, old_name TEXT)")
        con.execute("INSERT INTO widgets VALUES ('w1', 'before')")
        con.commit()
        con.close()

        schema = ("CREATE TABLE IF NOT EXISTS widgets (\n"
                  "    id       TEXT PRIMARY KEY,\n"
                  "    old_name TEXT,\n"
                  "    new_name TEXT NOT NULL\n"
                  ");\n")
        with mock.patch.object(engine_mod, "schema_sql", return_value=schema):
            database = Database.from_url(f"sqlite:///{path}")
            with self.assertRaises(DatabaseError) as ctx:
                database.migrate()
            message = str(ctx.exception)
            database.dispose()

        self.assertIn("widgets.new_name", message)
        self.assertIn("1 row", message)
        self.assertIn("renamed", message)

    def test_not_null_column_is_added_to_an_empty_table(self):
        """The same column is fine when there are no rows to violate it."""
        import sqlite3
        from backend.db import engine as engine_mod

        path = self.tmp / "empty.sqlite3"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, old_name TEXT)")
        con.commit()
        con.close()

        schema = ("CREATE TABLE IF NOT EXISTS widgets (\n"
                  "    id       TEXT PRIMARY KEY,\n"
                  "    old_name TEXT,\n"
                  "    new_name TEXT NOT NULL\n"
                  ");\n")
        # verify_schema checks the real tables against models.py; this stub
        # schema deliberately has none of them, and is not what is under test.
        with mock.patch.object(engine_mod, "schema_sql", return_value=schema), \
                mock.patch.object(Database, "verify_schema", lambda self: None):
            Database.from_url(f"sqlite:///{path}").migrate().dispose()

        con = sqlite3.connect(str(path))
        cols = {r[1] for r in con.execute("PRAGMA table_info(widgets)")}
        con.close()
        self.assertIn("new_name", cols)

    def test_existing_rows_survive_migration(self):
        import sqlite3
        path = self.tmp / "legacy2.sqlite3"
        con = sqlite3.connect(str(path))
        con.execute("""CREATE TABLE tracks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, artist TEXT NOT NULL,
            genre TEXT NOT NULL, license TEXT NOT NULL,
            license_nd INTEGER NOT NULL, license_sa INTEGER NOT NULL,
            license_nc INTEGER NOT NULL, mixable INTEGER NOT NULL,
            native_bpm REAL, camelot TEXT, duration_s REAL, audio_key     TEXT,
            analysis_json TEXT, segments_json TEXT)""")
        con.execute("INSERT INTO tracks VALUES ('keep','N','A','house',"
                    "'CC BY 4.0',0,0,0,1,124.0,'8A',60.0,'/tmp/x.wav',NULL,NULL)")
        con.commit()
        con.close()

        database = Database.from_url(f"sqlite:///{path}").migrate()
        try:
            with database.reading() as q:
                row = q.get_track(id="keep")
            self.assertIsNotNone(row)
            self.assertEqual(row.name, "N")
            self.assertEqual(row.status, status.PENDING)  # column default
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()
