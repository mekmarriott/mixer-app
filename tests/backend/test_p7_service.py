"""Service smoke test — the app booting the way a deployment boots it.

Everything else in the suite exercises modules. This starts the real
application: schema creation, catalog ingestion, warmup, and the endpoints a
browser actually calls, against **PostgreSQL** rather than SQLite and with a
**synthetic catalog** rather than Jamendo. That combination is what CI runs and
what deployment uses, and nothing else covers it end to end.

Synthetic on purpose. `config/tracks.json` ships in `jamendo` mode, so booting
it needs network and credentials; CI has neither and should not spend metered
API quota to prove the server starts. `mode: offline` generates the same
pipeline inputs deterministically (backend/synth.py), so ingestion, analysis,
variant rendering and serving are all genuinely exercised.

Skipped unless DJMIXER_SMOKE_DATABASE_URL names a database this test may own
outright — it ingests into it and the schema must be its own. Point it at a
database used for nothing else:

    createdb djmixer_smoke
    DJMIXER_SMOKE_DATABASE_URL=postgresql://localhost:5433/djmixer_smoke \\
        python -m unittest discover -s tests/backend -t tests/backend \\
                                    -p 'test_p7_*.py'

or `make test-smoke`.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

# The repository root, so `backend` imports. The other modules get this from
# fixture.py, which is deliberately not imported here: it builds the shared
# SQLite fixture catalog on import, and this module wants a database of its own.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

SMOKE_URL = os.environ.get("DJMIXER_SMOKE_DATABASE_URL")

# Two house tracks that share a grid point and one ND track, which is the
# smallest catalog that still exercises the compliance gate, the variant
# renderer and the recommendation path.
CATALOG = {
    "mode": "offline",
    "tracks": [
        {"id": "9001", "name": "Smoke One", "artist": "CI", "genre": "house",
         "bpm": 124, "key": "8A", "license": "CC BY 4.0", "duration_s": 12},
        {"id": "9002", "name": "Smoke Two", "artist": "CI", "genre": "house",
         "bpm": 126, "key": "9A", "license": "CC BY-SA 4.0", "duration_s": 12},
        {"id": "9003", "name": "Smoke ND", "artist": "CI", "genre": "house",
         "bpm": 125, "key": "8A", "license": "CC BY-ND 4.0", "duration_s": 12},
    ],
}


@unittest.skipUnless(SMOKE_URL, "set DJMIXER_SMOKE_DATABASE_URL to run")
class TestServiceBoot(unittest.TestCase):
    """One boot, then assertions against the running app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="djsmoke_"))
        cls.catalog = cls.tmp / "tracks.json"
        cls.catalog.write_text(json.dumps(CATALOG))

        # Set before importing config: DATA_DIR and TRACKS_CONFIG are resolved
        # from the environment at import time.
        os.environ["DJMIXER_DATA"] = str(cls.tmp)
        os.environ["DJMIXER_TRACKS"] = str(cls.catalog)
        os.environ["DJMIXER_DATABASE_URL"] = SMOKE_URL
        os.environ.setdefault("BLOB_BACKEND", "local")

        from backend import config, storage
        config.DATA_DIR = cls.tmp
        config.AUDIO_DIR = cls.tmp / "audio"
        config.VARIANT_DIR = cls.tmp / "variants"
        config.TRACKS_CONFIG = cls.catalog
        config.ensure_dirs()
        storage.reset_store()

        from backend.db import Database
        cls.database = Database.from_url(SMOKE_URL)
        cls._drop_everything(cls.database)
        cls.database.migrate()

        from backend.app import create_app
        app = create_app(run_ingestion=True, database=cls.database)
        app.config["TESTING"] = True
        cls.client = app.test_client()

    @classmethod
    def _drop_everything(cls, database):
        """Start from an empty schema so the boot is a real first boot.

        Ingestion is resumable: against a database left over from a previous
        run it would skip every track as already `ready` and this test would
        assert against a catalog it did not build.
        """
        with database.writing() as q:
            for table in ("mix_tracks", "mixes", "variants", "latency", "tracks"):
                q._conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    @classmethod
    def tearDownClass(cls):
        cls.database.dispose()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for var in ("DJMIXER_DATA", "DJMIXER_TRACKS", "DJMIXER_DATABASE_URL"):
            os.environ.pop(var, None)

    # -- the database is really PostgreSQL ---------------------------------
    def test_running_on_postgres(self):
        """Guard against the URL being ignored and SQLite used silently — the
        whole point of this module is the dialect it did not run under."""
        self.assertEqual(self.database.dialect, "postgres")

    # -- ingestion happened ------------------------------------------------
    def test_catalog_was_ingested(self):
        body = self.client.get("/api/ingest").get_json()
        self.assertTrue(body["complete"], body)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["counts"], {"ready": len(CATALOG["tracks"])})

    def test_analysis_ran_on_every_track(self):
        for track in self.client.get("/api/tracks").get_json():
            self.assertGreater(track["bpm"], 0, track["id"])
            self.assertTrue(track["camelot"], track["id"])

    def test_nd_track_has_no_variants(self):
        """The compliance gate, through the real API rather than the model."""
        by_id = {t["id"]: t for t in self.client.get("/api/tracks").get_json()}
        self.assertFalse(by_id["9003"]["mixable"])
        self.assertEqual(by_id["9003"]["grid_bpms"], [])
        self.assertTrue(by_id["9001"]["grid_bpms"])

    # -- the endpoints a browser calls -------------------------------------
    def test_health_and_status(self):
        self.assertTrue(self.client.get("/api/health").get_json()["ready"])
        self.assertEqual(self.client.get("/api/status").get_json()["phase"], "ready")

    def test_deck_is_populated(self):
        groups = self.client.get("/api/deck").get_json()["groups"]
        self.assertTrue(groups)
        self.assertTrue(any(g["tracks"] for g in groups))

    def test_audio_resolves(self):
        r = self.client.get("/api/tracks/9001/audio")
        self.assertIn(r.status_code, (200, 302))

    def test_waveform_comes_from_cached_analysis(self):
        body = self.client.get("/api/tracks/9001/waveform?points=64").get_json()
        self.assertEqual(len(body["points"]), 64)

    def test_recommendations_and_transitions(self):
        recs = self.client.get("/api/tracks/9001/recommendations").get_json()
        self.assertTrue(recs, "no recommendation for a track with a compatible pair")
        other = recs[0]["track_id"]
        curve = self.client.get(f"/api/transitions?a=9001&b={other}").get_json()
        self.assertTrue(curve["markers"])

    def test_index_page_serves(self):
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
