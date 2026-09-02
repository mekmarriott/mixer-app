"""Shared test fixture: ingests a 5-track catalog once per test run into a
temp directory. Includes BY / BY-SA / BY-NC / BY-ND licenses and a
cross-genre track so compliance gates and grid logic are all exercised."""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import teststore  # noqa: E402

from backend import config  # noqa: E402

FIXTURE_TRACKS = [
    {"id": "1001", "name": "Neon Corridor", "artist": "Volt Array", "genre": "house",
     "bpm": 124, "key": "8A", "license": "CC BY 4.0", "duration_s": 60},
    {"id": "1002", "name": "Glass Elevator", "artist": "Mira Flux", "genre": "house",
     "bpm": 126, "key": "7A", "license": "CC BY-SA 4.0", "duration_s": 60},
    {"id": "1003", "name": "Midnight Grid", "artist": "Kestrel Motive", "genre": "house",
     "bpm": 122, "key": "8B", "license": "CC BY-NC 4.0", "duration_s": 60},
    {"id": "1005", "name": "Locked Groove", "artist": "Pale Circuit", "genre": "house",
     "bpm": 124, "key": "8A", "license": "CC BY-ND 4.0", "duration_s": 60},
    {"id": "2001", "name": "Slow Ferry", "artist": "Harbor Lights", "genre": "downtempo",
     "bpm": 90, "key": "4A", "license": "CC BY 4.0", "duration_s": 60},
]

_cache = {}


def get_fixture():
    """Returns (database, results, tmpdir). Built once, reused by all modules."""
    if "database" in _cache:
        return _cache["database"], _cache["results"], _cache["tmp"]

    # Storage is chosen by the suite, never inherited from the environment or
    # from the committed .env.local — see tests/backend/teststore.py.
    tmp = teststore.isolate(config)

    from backend import ingest
    from backend.db import Database
    from backend.timing import Timer
    database = Database.from_config().migrate()
    timer = Timer(database)
    results = [ingest.ingest_track(database, e, "offline", timer)
               for e in FIXTURE_TRACKS]

    _cache.update(database=database, results=results, tmp=tmp, timer=timer)

    def cleanup():
        database.dispose()
        shutil.rmtree(tmp, ignore_errors=True)

    atexit.register(cleanup)
    return database, results, tmp


def read():
    """A read scope on the fixture database, for use as a context manager:

        with read() as q:
            track = q.get_track(id="1001")
    """
    database, _, _ = get_fixture()
    return database.reading()


def spec(track_id):
    return next(t for t in FIXTURE_TRACKS if t["id"] == track_id)
