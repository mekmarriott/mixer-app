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
    audio_path    TEXT,
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
    ready_at      REAL
);

CREATE TABLE IF NOT EXISTS variants (
    track_id   TEXT NOT NULL REFERENCES tracks (id) ON DELETE CASCADE,
    grid_bpm   INTEGER NOT NULL,
    ratio      REAL NOT NULL,
    path       TEXT NOT NULL,
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

-- Supports the recommendation pre-filter (docs/latency-report.md §Projections):
-- candidates are mixable tracks, narrowed by genre bucket and Camelot key.
CREATE INDEX IF NOT EXISTS idx_tracks_match ON tracks (mixable, genre, camelot);
CREATE INDEX IF NOT EXISTS idx_latency_stage ON latency (stage);

-- Startup ingestion asks "what is not ready yet?" once per run.
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status);
