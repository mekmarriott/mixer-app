-- Canonical catalog schema — the single source of truth.
--
-- Column types are *canonical* tokens, not engine types. backend/db/dialect.py
-- maps each token to concrete DDL for SQLite (local/dev) and PostgreSQL
-- (Supabase, in deployment); backend/db/codegen.py parses this file to derive
-- the generated dataclasses in models.py and to type query parameters.
--
--   TEXT      text                      JSONDOC   JSON document (TEXT / JSONB)
--   INTEGER   whole number              BOOLEAN   true/false (INTEGER / BOOLEAN)
--   REAL      float                     IDENTITY  auto-assigned integer PK
--
-- After editing this file re-run:  python -m backend.db.codegen

CREATE TABLE IF NOT EXISTS tracks (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    artist        TEXT NOT NULL,
    genre         TEXT NOT NULL,
    license       TEXT NOT NULL,
    license_nd    BOOLEAN NOT NULL,
    license_sa    BOOLEAN NOT NULL,
    license_nc    BOOLEAN NOT NULL,
    mixable       BOOLEAN NOT NULL,
    native_bpm    REAL,
    camelot       TEXT,
    duration_s    REAL,
    audio_key     TEXT,
    analysis_json JSONDOC,
    segments_json JSONDOC,
    -- Ingestion progress. `status` is a HIGH-WATER MARK of completed work
    -- (pending -> fetched -> analyzed -> ready) so a rerun can skip every
    -- stage already durably on disk. Failure lives in `status_error` rather
    -- than in `status` on purpose: overwriting the mark with 'failed' would
    -- lose the record of the download that did succeed, and the retry would
    -- fetch the audio all over again.
    status        TEXT NOT NULL DEFAULT 'pending',
    status_error  TEXT,
    source_url    TEXT,
    fetched_at    REAL,
    analyzed_at   REAL,
    ready_at      REAL,
    -- Energy of the regions a transition actually joins: the mean of the
    -- analysis prefix RMS over the LAST segment (what a mix fades out of) and
    -- over the FIRST (what it fades into). Derived from analysis_json and
    -- segments_json at ingest, and stored because scoring needs nothing else
    -- from those blobs.
    --
    -- Ranking one track against the catalog previously read both blobs for
    -- every candidate — megabytes each — to reduce them to exactly these two
    -- numbers. As columns they ride along with the summary row that scoring
    -- already reads, so the ranking touches no blob at all.
    --
    -- Appended, not inserted: rows are decoded positionally, so column order
    -- is load-bearing and only the end of the table is safe to extend. A live
    -- table predating these gets them from migrate(), with NULL for rows that
    -- have not been re-ingested; matching treats NULL as "fall back to the
    -- blobs" rather than as an energy of zero.
    outro_energy  REAL,
    intro_energy  REAL,
    -- The deck-sized amplitude envelope, DECK_WAVEFORM_POINTS long.
    --
    -- Every deck row shows one, and the suggestion deck is drawn from tracks
    -- chosen at request time, so the alternative to storing it is deriving it
    -- per request from analysis_json — a megabyte read and a downsample per
    -- row, twenty rows to a response. WaveformCache holds these too, but it is
    -- an in-process dict: cold on every new serverless instance, which is
    -- exactly when a first visitor is waiting.
    deck_waveform JSONDOC,
    -- The full envelope() result at native tempo, TIMELINE_WAVEFORM_POINTS long.
    --
    -- Stored whole rather than as points alone because the timing metadata
    -- beside them — duration, hop, beat grid — is what makes it reusable: the
    -- sampled points do not depend on the grid a track is stretched to, only
    -- those scalars do, and they scale by a ratio. So one stored native
    -- envelope answers a request at ANY grid BPM exactly, not approximately,
    -- and the analysis blob is read at ingest instead of per request.
    native_envelope JSONDOC,
    -- Total listens on Jamendo, all time. Ordering the opening view needs a
    -- measure of what is worth hearing first, and this is the one the source
    -- already returns with every track.
    popularity    INTEGER
);

CREATE TABLE IF NOT EXISTS variants (
    track_id   TEXT NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    grid_bpm   INTEGER NOT NULL,
    ratio      REAL NOT NULL,
    object_key TEXT NOT NULL,
    duration_s REAL NOT NULL,
    PRIMARY KEY (track_id, grid_bpm)
);

CREATE TABLE IF NOT EXISTS latency (
    id       IDENTITY,
    stage    TEXT NOT NULL,
    track_id TEXT,
    ms       REAL NOT NULL,
    at       REAL NOT NULL
);

-- A saved mix. `head_id` is the first node of the track chain; NULL means the
-- mix exists but is empty (the zero state).
CREATE TABLE IF NOT EXISTS mixes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    head_id    TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- One track's placement within a mix, as a singly-linked list.
--
-- Why a linked list rather than a position column: an insert or delete in the
-- middle of a long mix repoints one `next_id` instead of renumbering every
-- following row. The cost is that ordering cannot be expressed in SQL — a mix
-- is read whole and walked in Python (bounded at 100 nodes), and the walk
-- guards against cycles and orphans.
--
-- Why `delta_beats` rather than a position in seconds: it is the gap from the
-- PREVIOUS track's start, in whole beats at `grid_bpm`. Relative means a
-- ripple edit rewrites ONE row and the rest of the chain moves with it.
-- Integer beats mean an off-grid placement is not representable at all, which
-- is what makes beat alignment structural rather than merely enforced in the
-- UI. Seconds are derived: delta_beats * 60 / grid_bpm.
CREATE TABLE IF NOT EXISTS mix_tracks (
    id          TEXT PRIMARY KEY,
    mix_id      TEXT NOT NULL REFERENCES mixes (id) ON DELETE CASCADE,
    track_id    TEXT NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    next_id     TEXT,
    delta_beats INTEGER NOT NULL,
    grid_bpm    INTEGER NOT NULL,
    -- How long this track's fade-IN runs, which is also how long its
    -- predecessor's fade-OUT runs — one crossfade, described once, owned by
    -- the track being brought in.
    --
    -- Stored with the chain because it decides when the PREVIOUS track goes
    -- silent, and that is what bounds legal placement. Re-deriving it would
    -- mean re-scoring the junction on every chain read. NULL means "not
    -- known", and callers then fall back to the track playing out in full.
    fade_s      REAL
);

-- Supports the recommendation pre-filter (docs/latency-report.md §Projections):
-- candidates are mixable tracks, narrowed by genre bucket and Camelot key.
CREATE INDEX IF NOT EXISTS idx_tracks_match ON tracks (mixable, genre, camelot);
CREATE INDEX IF NOT EXISTS idx_latency_stage ON latency (stage);

-- Startup ingestion asks "what is not ready yet?" once per run.
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status);
-- A mix is always loaded whole, so the chain walk is one indexed read.
CREATE INDEX IF NOT EXISTS idx_mix_tracks_mix ON mix_tracks (mix_id);
-- The mix picker lists most-recently-edited first.
CREATE INDEX IF NOT EXISTS idx_mixes_updated ON mixes (updated_at);
