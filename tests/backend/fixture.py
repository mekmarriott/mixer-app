"""Shared test fixture: ingests a 5-track catalog once per test run into a
temp directory. Includes BY / BY-SA / BY-NC / BY-ND licenses and a
cross-genre track so compliance gates and grid logic are all exercised."""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
    """Returns (con, results, tmpdir). Built once, reused by all test modules."""
    if "con" in _cache:
        return _cache["con"], _cache["results"], _cache["tmp"]

    tmp = Path(tempfile.mkdtemp(prefix="djtest_"))
    config.DATA_DIR = tmp
    config.AUDIO_DIR = tmp / "audio"
    config.VARIANT_DIR = tmp / "variants"
    config.DB_PATH = tmp / "test.sqlite3"
    config.ensure_dirs()

    from backend import db, ingest
    from backend.timing import Timer
    con = db.connect()
    timer = Timer(con)
    results = [ingest.ingest_track(con, e, "offline", timer) for e in FIXTURE_TRACKS]

    _cache.update(con=con, results=results, tmp=tmp, timer=timer)
    atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return con, results, tmp


def spec(track_id):
    return next(t for t in FIXTURE_TRACKS if t["id"] == track_id)
