"""Where the test suites are allowed to keep data.

Tests own their storage. They do not inherit it.

`.env.local` is committed and sets `DJMIXER_DATABASE_URL` to the shared local
PostgreSQL so that a local run has the same shape as production. `backend/
config.py` loads that file at import, which means anything importing the
backend picks the developer's catalog up by default — including a test fixture.
The failure is quiet and expensive: the suite builds its fixture in the dev
database, sees tracks it has never heard of, fails a dozen assertions for
reasons that look like a code regression, and leaves its own rows behind.

Relying on the caller to blank the variable (`DJMIXER_DATABASE_URL= python -m
unittest`) is not a fix, because the unsafe invocation is the short one anyone
would type. So the decision lives here instead, and it can only ever resolve to
storage dedicated to testing:

  * `DJMIXER_TEST_DATABASE_URL` — an explicit, deliberately separate variable.
    `make test-pg` sets it to the `djmixer_test` database. Nothing else does.
  * otherwise a throwaway SQLite file in a temp directory, deleted at exit.

`DJMIXER_DATABASE_URL` is ignored either way, and a test URL that names the
development database is refused rather than used.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

# Importable before `backend` is on the path, so a test module can pin its
# storage as its very first act.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: The one variable a test run may use to select a database. Deliberately NOT
#: DJMIXER_DATABASE_URL: that is the application's, and .env.local sets it.
TEST_URL_VAR = "DJMIXER_TEST_DATABASE_URL"

#: A test database has to say so in its name. This is the guard against a
#: mistyped Makefile pointing the suite at real data.
TEST_DB_NAME = re.compile(r"(test|_e2e|ci)", re.IGNORECASE)


class UnsafeTestStore(RuntimeError):
    """The configured test database is not a test database."""


def test_database_url():
    """The URL the suite may use, or None to mean 'a temp SQLite file'."""
    url = (os.environ.get(TEST_URL_VAR) or "").strip()
    if not url:
        return None

    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    if not TEST_DB_NAME.search(name):
        raise UnsafeTestStore(
            f"{TEST_URL_VAR} points at database {name!r}, which is not named as a "
            f"test database. The suite creates, mutates and deletes rows freely, "
            f"so it must never run against development or production data. Use a "
            f"dedicated database (make test-pg uses 'djmixer_test'), or unset the "
            f"variable to run on throwaway SQLite.")
    return url


def isolate(config, tmp=None):
    """Point `backend.config` at storage dedicated to this test run.

    Returns the temp directory, which the caller is responsible for removing.
    Every path AND the database URL are overridden, so nothing an ambient
    environment or a committed .env.local says can reach real data.
    """
    tmp = Path(tmp or tempfile.mkdtemp(prefix="djtest_"))
    config.DATA_DIR = tmp
    config.AUDIO_DIR = tmp / "audio"
    config.VARIANT_DIR = tmp / "variants"
    config.DB_PATH = tmp / "test.sqlite3"

    # The important line: whatever config resolved at import from the
    # environment or .env.local is discarded in favour of the test's own store.
    config.DATABASE_URL = test_database_url()

    config.ensure_dirs()
    return tmp


def describe():
    """One line naming the store in use, for a test runner to print."""
    url = test_database_url()
    return f"PostgreSQL ({url.rsplit('/', 1)[-1]})" if url else "throwaway SQLite"
