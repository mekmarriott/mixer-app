"""Central configuration for the DJ mixer backend."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- credentials
# Two files, loaded in precedence order:
#
#   .env         gitignored. Credentials and personal overrides.
#   .env.local   COMMITTED. Shared local endpoints — the local Postgres URL,
#                the blob backend — so a checkout runs in the same shape as
#                production instead of silently falling back to SQLite and
#                filesystem paths.
#
# `load_env_file` uses setdefault, so first writer wins: real environment
# variables beat .env, which beats .env.local.
ENV_FILE = Path(os.environ.get("DJMIXER_ENV_FILE", ROOT / ".env"))
LOCAL_ENV_FILE = Path(os.environ.get("DJMIXER_LOCAL_ENV_FILE", ROOT / ".env.local"))

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


load_env_file(ENV_FILE)             # secrets first: they win over the defaults
load_env_file(LOCAL_ENV_FILE)       # committed local endpoints

# Jamendo credentials. JAMENDO_API_CLIENT is the primary name: it is what
# Jamendo's own dashboard calls the field, so a .env pasted straight from
# there works unedited, and it is what this project's .env and Vercel project
# actually use. JAMENDO_CLIENT_ID stays accepted for anyone following older
# docs, but it is the alias now, not the canonical name.
JAMENDO_CLIENT_ID_VARS = ("JAMENDO_API_CLIENT", "JAMENDO_CLIENT_ID")


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

#: Fallbacks for the database URL, in order, when DJMIXER_DATABASE_URL is unset.
#:
#: Vercel's Supabase integration injects its own names and does not know ours,
#: so on a deployment the connection string is already present — under a name
#: nothing reads. Requiring DJMIXER_DATABASE_URL as well means hand-copying a
#: live credential into a second variable, which is both a chore and a way to
#: end up with two values that disagree. Reading the injected name directly
#: removes the duplicate.
#:
#: Pooled first: Supabase's transaction pooler (port 6543) is what a serverless
#: function must use — see docs/infrastructure-plan.md §1.3. The non-pooling
#: URL is a last resort, correct for a long-lived process and wrong for this one.
DATABASE_URL_FALLBACK_VARS = (
    "MIX_DB_POSTGRES_URL",              # Supabase integration, pooled
    "MIX_DB_POSTGRES_PRISMA_URL",       # same host, pgbouncer flag
    "MIX_DB_POSTGRES_URL_NON_POOLING",
)


def database_url():
    """The database URL in force. See docs/database.md.

    DJMIXER_DATABASE_URL wins. Failing that, a URL injected by the Supabase
    integration is used, so a deployment needs no duplicated credential.
    Failing that, the local SQLite file — which is right for development and
    wrong for a read-only serverless filesystem, so the caller that cares
    should check `is_local_sqlite()`.
    """
    if DATABASE_URL:
        return DATABASE_URL
    # Set but EMPTY is an explicit opt-out, not an absent value: run_tests.sh
    # and the CI workflow pass `DJMIXER_DATABASE_URL=` precisely to force the
    # local SQLite path. Falling through to an injected URL there would point
    # the suite at the production database.
    if DATABASE_URL == "":
        return "sqlite:///" + str(DB_PATH)
    for var in DATABASE_URL_FALLBACK_VARS:
        url = (os.environ.get(var) or "").strip()
        if url.startswith(("postgres://", "postgresql://")):
            return url
    return "sqlite:///" + str(DB_PATH)


def is_local_sqlite():
    """True when the backend would fall back to the on-disk SQLite catalog."""
    return database_url().startswith("sqlite:")

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

# BPM grid buckets (bucket name -> integer grid points).
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
#
# GRID SPACING trades storage against compatibility, and the cost is small but
# NOT zero. Two tracks are mix-compatible when they share a grid point. Inside
# a band narrower than roughly a 1.2 max/min ratio, coarsening costs nothing:
# the reachable span is set by MAX_STRETCH_RATIO rather than by the spacing, so
# every track still reaches every point near it. In a wider band, two tracks
# near opposite edges can only meet on an interior point, and a coarse grid may
# not put one where both can reach.
#
# Measured against a 1 BPM grid, at spacing 5: `slow` loses 2.96% of pairs and
# `midtempo` 2.62%; downtempo, house, uptempo and fast lose none. Worst-case
# joint stretch rises about a point (3.33% -> 4.17% across house).
# tests/backend/test_p5_deploy.py asserts those bounds, so raising the spacing
# further has to be a deliberate trade rather than a silent regression.
#
# What it buys is roughly 4x fewer rendered variants per track — about 75% of
# all stored audio bytes, and the same fraction of ingestion compute.
# See docs/infrastructure-plan.md §4.3.
GRID_SPACING = 5

# A band's *extent* and its *grid points* are separate things. Extent is the
# span of tempos that belong to the band, and is what bucket_for_bpm assigns
# from; grid points are the discrete BPMs variants are rendered at inside it.
# They coincided while the spacing was 1 BPM, which is why the two were once
# one list — but at spacing 5 the last grid point is no longer the top of the
# band (slow ends at 84 while its last point is 80), and deriving extent from
# the points would leave every band with an uncovered tail.
BPM_BANDS = {
    "slow":      (70, 84),
    "downtempo": (85, 95),
    "midtempo":  (96, 119),
    "house":     (120, 128),
    "uptempo":   (129, 152),
    "fast":      (153, 182),
}

BPM_BUCKETS = {name: list(range(lo, hi + 1, GRID_SPACING))
               for name, (lo, hi) in BPM_BANDS.items()}
MAX_STRETCH_RATIO = 0.10                # hard cap; beyond this a variant is not rendered


def bucket_for_bpm(bpm):
    """The tempo band a detected BPM belongs to.

    Used when curating a catalog from a source with no usable tempo metadata.
    Bands are defined as integer grids but detected BPMs are continuous, so
    each band claims the half-open real interval around its integers — a
    measured 119.96 belongs to the band starting at 120, not to nowhere.
    Returns None only for a BPM outside every band's range."""
    for name, (lo, hi) in BPM_BANDS.items():
        if lo - 0.5 <= bpm < hi + 0.5:
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
