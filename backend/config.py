"""Central configuration for the DJ mixer backend."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- credentials
ENV_FILE = Path(os.environ.get("DJMIXER_ENV_FILE", ROOT / ".env"))

# `export KEY=value`, `KEY=value`, optional quotes; blank lines and # ignored.
_ENV_LINE = re.compile(
    r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")


def load_env_file(path=ENV_FILE):
    """Read a .env file into os.environ without clobbering the real environment.

    Keeps API credentials out of the repo (.env is gitignored) while letting
    `python3 -m backend.app` work with no shell setup. Real environment
    variables always win, so CI and containers can override the file."""
    loaded = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return loaded
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        loaded[key] = val
        os.environ.setdefault(key, val)
    return loaded


load_env_file()

# Jamendo credentials. JAMENDO_CLIENT_ID is the documented name;
# JAMENDO_API_CLIENT is what Jamendo's own dashboard calls it, and is accepted
# so a .env copied straight from there works unedited.
JAMENDO_CLIENT_ID_VARS = ("JAMENDO_CLIENT_ID", "JAMENDO_API_CLIENT")


def jamendo_client_id():
    """The configured Jamendo client id, or None."""
    for var in JAMENDO_CLIENT_ID_VARS:
        val = os.environ.get(var)
        if val:
            return val.strip()
    return None


DATA_DIR = Path(os.environ.get("DJMIXER_DATA", ROOT / "data"))
AUDIO_DIR = DATA_DIR / "audio"          # original masters
VARIANT_DIR = DATA_DIR / "variants"     # tempo-matched renders
DB_PATH = DATA_DIR / "catalog.sqlite3"
TRACKS_CONFIG = Path(os.environ.get("DJMIXER_TRACKS", ROOT / "config" / "tracks.json"))

#: Set to a Postgres connection string to run against Supabase in deployment;
#: unset, the backend uses the local SQLite file at DB_PATH. Read through
#: database_url() rather than directly — DB_PATH is reassigned by the test
#: fixture and the benchmark, so the default has to be computed on demand.
DATABASE_URL = os.environ.get("DJMIXER_DATABASE_URL")


def database_url():
    """The database URL in force. See docs/database.md."""
    if DATABASE_URL:
        return DATABASE_URL
    return "sqlite:///" + str(DB_PATH)

SAMPLE_RATE = 22050                     # prototype rate (see design doc §Audio format)

# ---- DB concurrency (backend/dbpool.py) ---------------------------------
# max_concurrency must stay STRICTLY BELOW pool_size: the semaphore is the
# queueing point, and the spare connections guarantee an admitted caller never
# blocks on checkout. Raising these past what the storage engine allows
# concurrently is the thing to avoid — for Postgres that is max_connections
# minus whatever the ingest workers hold.
DB_POOL_SIZE = 8
DB_MAX_CONCURRENCY = 6
DB_ACQUIRE_TIMEOUT_S = 5.0

# ---- Zero-state deck ----------------------------------------------------
# Before any track is chosen there is nothing to match against, so the opening
# view is a browse surface: a few tracks per genre, no pair analysis.
DECK_TRACKS_PER_GENRE = 5
DECK_WAVEFORM_POINTS = 120              # deck row thumbnails
TIMELINE_WAVEFORM_POINTS = 480          # track window
FRAME_SIZE = 2048
HOP_SIZE = 512

# BPM grid buckets (bucket name -> inclusive integer grid).
#
# The bucket is a *tempo* concept, not really a genre one: it exists so two
# tracks in the same bucket can meet at a shared grid BPM within the stretch
# cap. `house` and `downtempo` are named for genres because those genres are
# tightly tempo-correlated; the remaining bands are named for what they
# actually are, since a heterogeneous pop catalog spans tempos that no genre
# label predicts.
#
# Bands are kept near or below a 1.2 max/min ratio so that most pairs within a
# band can actually meet at +/-10%. Where a band is wider than that, the far
# ends simply share no grid point and are never recommended to each other —
# handled by `bpm_grid.shared_grid`, not a special case.
BPM_BUCKETS = {
    "slow":      list(range(70, 85)),     # 70..84
    "downtempo": list(range(85, 96)),     # 85..95
    "midtempo":  list(range(96, 120)),    # 96..119
    "house":     list(range(120, 129)),   # 120..128
    "uptempo":   list(range(129, 153)),   # 129..152
    "fast":      list(range(153, 183)),   # 153..182
}
MAX_STRETCH_RATIO = 0.10                # hard cap; beyond this a variant is not rendered


def bucket_for_bpm(bpm):
    """The tempo band a detected BPM belongs to.

    Used when curating a catalog from a source with no usable tempo metadata.
    Bands are defined as integer grids but detected BPMs are continuous, so
    each band claims the half-open real interval around its integers — a
    measured 119.96 belongs to the band starting at 120, not to nowhere.
    Returns None only for a BPM outside every band's range."""
    for name, grid in BPM_BUCKETS.items():
        if grid[0] - 0.5 <= bpm < grid[-1] + 0.5:
            return name
    return None

# Matching score weights (Phase 2)
WEIGHT_BPM = 0.45
WEIGHT_KEY = 0.35
WEIGHT_ENERGY = 0.20
MATCH_SCORE_CUTOFF = 0.40               # below this a candidate is not recommended

# Transition detection (Phase 3)
WINDOW_BARS = 8                          # transition window length in bars
HOP_BARS = 1                             # sliding hop in bars
MARKER_TOP_N = 5                         # markers surfaced to the UI

CREDITS = [
    {"name": "Essentia", "license": "AGPL-3.0", "url": "https://essentia.upf.edu/licensing_information.html",
     "note": "Audio analysis: BPM, beat grid, key, frame features. "
             "numpy/scipy fallback DSP used only when Essentia is unavailable."},
    {"name": "Rubber Band Library", "license": "GPL-2.0 / commercial", "url": "https://breakfastquay.com/rubberband/",
     "note": "Offline time-stretch (R3 engine) for BPM-grid variants. "
             "scipy phase-vocoder fallback used only when unavailable."},
    {"name": "Jamendo", "license": "API terms + per-track CC licenses",
     "url": "https://developer.jamendo.com/v3.0",
     "note": "Track source. Only audiodownload_allowed tracks are ingested; "
             "each track's own CC license is read from the API and enforced."},
    {"name": "FFmpeg", "license": "LGPL-2.1+ / GPL-2+", "url": "https://ffmpeg.org",
     "note": "Decodes Jamendo MP3s to mono PCM at the pipeline sample rate."},
    {"name": "wavesurfer.js", "license": "BSD-3-Clause", "url": "https://wavesurfer.xyz",
     "note": "Not bundled in the prototype (custom canvas renderer); listed for the production plan."},
    {"name": "Tone.js", "license": "MIT", "url": "https://tonejs.github.io",
     "note": "Not bundled in the prototype (native Web Audio scheduling); listed for the production plan."},
    {"name": "NumPy", "license": "BSD-3-Clause", "url": "https://numpy.org"},
    {"name": "SciPy", "license": "BSD-3-Clause", "url": "https://scipy.org"},
    {"name": "Flask", "license": "BSD-3-Clause", "url": "https://flask.palletsprojects.com"},
]


def load_tracks_config():
    with open(TRACKS_CONFIG) as f:
        return json.load(f)


def ensure_dirs():
    for d in (DATA_DIR, AUDIO_DIR, VARIANT_DIR):
        d.mkdir(parents=True, exist_ok=True)
