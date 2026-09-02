"""Central configuration for the DJ mixer backend."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
FRAME_SIZE = 2048
HOP_SIZE = 512

# BPM grid buckets (genre -> inclusive integer grid). Stretch tolerance ~ +/-8%.
BPM_BUCKETS = {
    "house":     list(range(120, 129)),   # 120..128
    "downtempo": list(range(85, 96)),     # 85..95
}
MAX_STRETCH_RATIO = 0.10                # hard cap; beyond this a variant is not rendered

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
     "note": "Audio analysis (production). Offline fallback DSP used when Essentia is unavailable."},
    {"name": "Rubber Band Library", "license": "GPL-2.0 / commercial", "url": "https://breakfastquay.com/rubberband/",
     "note": "Offline time-stretch (production). scipy phase-vocoder fallback used when unavailable."},
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
