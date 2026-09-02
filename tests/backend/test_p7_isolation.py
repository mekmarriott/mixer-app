"""The suite must be unable to reach development data (ISO-01..04).

This is a guarantee about the tests themselves. `.env.local` is committed and
points `DJMIXER_DATABASE_URL` at the shared local PostgreSQL, so any test that
builds a database from config inherits the developer's catalog unless something
stops it. Something now does: tests/backend/teststore.py.

The bug this replaces was quiet and expensive — a dozen assertions failing for
reasons that looked like a code regression, and the suite's own rows left
behind in a real database.
"""
import os
import tempfile
import unittest
from pathlib import Path

import teststore

from backend import config


class TestStoreSelection(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("DJMIXER_DATABASE_URL", teststore.TEST_URL_VAR)}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------- ISO-01
    def test_iso_01_application_database_url_is_ignored(self):
        """The one that bit: .env.local's URL must not reach the suite."""
        os.environ["DJMIXER_DATABASE_URL"] = \
            "postgresql://127.0.0.1:5433/djmixer"
        os.environ.pop(teststore.TEST_URL_VAR, None)
        self.assertIsNone(teststore.test_database_url())

    def test_iso_01_isolate_overrides_a_configured_application_url(self):
        os.environ["DJMIXER_DATABASE_URL"] = \
            "postgresql://127.0.0.1:5433/djmixer"
        os.environ.pop(teststore.TEST_URL_VAR, None)
        saved = (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
                 config.DB_PATH, config.DATABASE_URL)
        config.DATABASE_URL = "postgresql://127.0.0.1:5433/djmixer"
        tmp = None
        try:
            tmp = teststore.isolate(config)
            self.assertTrue(config.database_url().startswith("sqlite:///"))
            self.assertIn(str(tmp), config.database_url())
        finally:
            (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
             config.DB_PATH, config.DATABASE_URL) = saved
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    def test_iso_01_isolate_beats_an_integration_injected_url(self):
        """The guarantee is about the RESOLVED database, not one variable.

        config.database_url() falls back to the URLs Vercel's Supabase
        integration injects (MIX_DB_POSTGRES_URL and friends) so a deployment
        needs no duplicated credential. Isolation has to survive that: leaving
        DATABASE_URL unset here would hand the suite the production database
        through a variable this module never reads.
        """
        os.environ.pop("DJMIXER_DATABASE_URL", None)
        os.environ.pop(teststore.TEST_URL_VAR, None)
        saved_env = {v: os.environ.get(v)
                     for v in config.DATABASE_URL_FALLBACK_VARS}
        saved = (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
                 config.DB_PATH, config.DATABASE_URL)
        tmp = None
        try:
            for v in config.DATABASE_URL_FALLBACK_VARS:
                os.environ[v] = "postgresql://prod.example.com:6543/postgres"
            tmp = teststore.isolate(config)
            self.assertTrue(config.is_local_sqlite(),
                            f"isolation leaked to {config.database_url()!r}")
            self.assertIn(str(tmp), config.database_url())
        finally:
            (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
             config.DB_PATH, config.DATABASE_URL) = saved
            for v, val in saved_env.items():
                if val is None:
                    os.environ.pop(v, None)
                else:
                    os.environ[v] = val
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------- ISO-02
    def test_iso_02_a_dedicated_test_database_is_accepted(self):
        for url in ("postgresql://u@127.0.0.1:5433/djmixer_test",
                    "postgresql://u@host/mixer_ci",
                    "postgresql://u@host/app_e2e"):
            os.environ[teststore.TEST_URL_VAR] = url
            self.assertEqual(teststore.test_database_url(), url)

    def test_iso_02_a_non_test_database_is_refused(self):
        """A mistyped Makefile must fail loudly, not silently use real data."""
        for url in ("postgresql://u@127.0.0.1:5433/djmixer",
                    "postgresql://u@prod.example.com/mixer",
                    "postgresql://u@host/production"):
            os.environ[teststore.TEST_URL_VAR] = url
            with self.assertRaises(teststore.UnsafeTestStore) as ctx:
                teststore.test_database_url()
            self.assertIn("not named as a test database", str(ctx.exception))

    def test_iso_02_the_refusal_explains_the_fix(self):
        os.environ[teststore.TEST_URL_VAR] = "postgresql://u@h/djmixer"
        with self.assertRaises(teststore.UnsafeTestStore) as ctx:
            teststore.test_database_url()
        msg = str(ctx.exception)
        self.assertIn("djmixer_test", msg)      # names the safe alternative
        self.assertIn("deletes rows", msg)      # says why it matters

    def test_iso_02_blank_and_whitespace_mean_sqlite(self):
        for value in ("", "   "):
            os.environ[teststore.TEST_URL_VAR] = value
            self.assertIsNone(teststore.test_database_url())

    # ------------------------------------------------------------- ISO-03
    def test_iso_03_isolate_redirects_every_path(self):
        saved = (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
                 config.DB_PATH, config.DATABASE_URL)
        os.environ.pop(teststore.TEST_URL_VAR, None)
        tmp = None
        try:
            tmp = teststore.isolate(config)
            for path in (config.DATA_DIR, config.AUDIO_DIR,
                         config.VARIANT_DIR, config.DB_PATH):
                self.assertTrue(str(path).startswith(str(tmp)),
                                f"{path} escaped the test directory")
            self.assertTrue(config.AUDIO_DIR.is_dir())
            self.assertTrue(config.VARIANT_DIR.is_dir())
        finally:
            (config.DATA_DIR, config.AUDIO_DIR, config.VARIANT_DIR,
             config.DB_PATH, config.DATABASE_URL) = saved
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------- ISO-04
    def test_iso_04_the_shared_fixture_lives_in_a_temp_directory(self):
        """End to end: the real fixture, however it was invoked."""
        from fixture import get_fixture
        database, _, tmp = get_fixture()
        self.assertTrue(str(tmp).startswith(tempfile.gettempdir()))
        url = database.engine.__class__.__name__
        if not teststore.test_database_url():
            self.assertEqual(url, "SQLiteEngine")
            self.assertTrue(str(config.DB_PATH).startswith(str(tmp)))

    def test_iso_04_describe_names_the_store_in_use(self):
        os.environ.pop(teststore.TEST_URL_VAR, None)
        self.assertEqual(teststore.describe(), "throwaway SQLite")
        os.environ[teststore.TEST_URL_VAR] = "postgresql://u@h/djmixer_test"
        self.assertEqual(teststore.describe(), "PostgreSQL (djmixer_test)")


if __name__ == "__main__":
    unittest.main()
