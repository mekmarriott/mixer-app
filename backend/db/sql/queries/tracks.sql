-- Track queries. sqlc-style annotations:
--
--   -- name: <QueryName> :one | :many | :exec | :scalar
--   -- columns: <name> <TYPE>, ...     (only when the SELECT list isn't `*`)
--
-- Parameters are named (`:id`); their canonical type is inferred from the
-- column of the same name on the table(s) the statement references.
-- Regenerate the Python bindings with:  python -m backend.db.codegen

-- name: GetTrack :one
SELECT * FROM tracks WHERE id = :id;

-- name: ListTracks :many
SELECT * FROM tracks ORDER BY id;

-- name: ListMixableTracks :many
SELECT * FROM tracks WHERE mixable = :mixable ORDER BY id;

-- The catalog listing needs metadata only. `SELECT *` would drag every track's
-- analysis and segment blobs through the driver to render a list that shows
-- neither, which is the dominant cost of the endpoint once the catalog grows.
-- name: ListTrackSummaries :many
-- columns: id TEXT, name TEXT, artist TEXT, genre TEXT, license TEXT,
--          license_nd BOOLEAN, license_sa BOOLEAN, license_nc BOOLEAN,
--          mixable BOOLEAN, native_bpm REAL, camelot TEXT, duration_s REAL,
--          status TEXT
SELECT id, name, artist, genre, license,
       license_nd, license_sa, license_nc,
       mixable, native_bpm, camelot, duration_s, status
FROM tracks
ORDER BY id;

-- name: CountTracks :scalar
-- columns: n INTEGER
SELECT COUNT(*) AS n FROM tracks;

-- name: GetTrackAnalysis :scalar
-- columns: analysis_json JSONDOC
SELECT analysis_json FROM tracks WHERE id = :id;

-- name: GetTrackSegments :scalar
-- columns: segments_json JSONDOC
SELECT segments_json FROM tracks WHERE id = :id;

-- Upsert rather than INSERT OR REPLACE: replace would delete the row first and
-- cascade away its rendered variants. DO UPDATE keeps re-ingestion idempotent.
-- name: UpsertTrack :exec
INSERT INTO tracks (id, name, artist, genre, license, license_nd, license_sa,
                    license_nc, mixable, native_bpm, camelot, duration_s,
                    audio_path, analysis_json, segments_json,
                    status, status_error, source_url,
                    fetched_at, analyzed_at, ready_at)
VALUES (:id, :name, :artist, :genre, :license, :license_nd, :license_sa,
        :license_nc, :mixable, :native_bpm, :camelot, :duration_s,
        :audio_path, :analysis_json, :segments_json,
        :status, :status_error, :source_url,
        :fetched_at, :analyzed_at, :ready_at)
ON CONFLICT (id) DO UPDATE SET
    name          = EXCLUDED.name,
    artist        = EXCLUDED.artist,
    genre         = EXCLUDED.genre,
    license       = EXCLUDED.license,
    license_nd    = EXCLUDED.license_nd,
    license_sa    = EXCLUDED.license_sa,
    license_nc    = EXCLUDED.license_nc,
    mixable       = EXCLUDED.mixable,
    native_bpm    = COALESCE(EXCLUDED.native_bpm, tracks.native_bpm),
    camelot       = COALESCE(EXCLUDED.camelot, tracks.camelot),
    -- COALESCE so an incremental write cannot blank what an earlier stage
    -- stored: ingestion upserts the row after fetch (no analysis yet) and
    -- again after analysis, and neither write may erase the other's columns.
    -- Only the always-supplied NOT NULL columns above overwrite unconditionally.
    duration_s    = COALESCE(EXCLUDED.duration_s, tracks.duration_s),
    audio_path    = COALESCE(EXCLUDED.audio_path, tracks.audio_path),
    analysis_json = COALESCE(EXCLUDED.analysis_json, tracks.analysis_json),
    segments_json = COALESCE(EXCLUDED.segments_json, tracks.segments_json),
    status        = EXCLUDED.status,
    status_error  = EXCLUDED.status_error,
    source_url    = COALESCE(EXCLUDED.source_url, tracks.source_url),
    fetched_at    = COALESCE(EXCLUDED.fetched_at, tracks.fetched_at),
    analyzed_at   = COALESCE(EXCLUDED.analyzed_at, tracks.analyzed_at),
    ready_at      = COALESCE(EXCLUDED.ready_at, tracks.ready_at);

-- Ingestion progress. Advancing the high-water mark clears any stale error;
-- recording an error deliberately leaves the mark alone so the retry resumes
-- from the last completed stage instead of starting over.
-- Clearing the cached analysis is the supported way to force a re-analysis on
-- the next run; the upsert deliberately cannot do it, since it COALESCEs.
-- name: ClearTrackAnalysis :exec
UPDATE tracks SET analysis_json = NULL, segments_json = NULL WHERE id = :id;

-- name: SetTrackStatus :exec
UPDATE tracks SET status = :status, status_error = NULL WHERE id = :id;

-- name: MarkTrackFailed :exec
UPDATE tracks SET status_error = :status_error WHERE id = :id;

-- name: StampTrackFetched :exec
UPDATE tracks SET fetched_at = :fetched_at WHERE id = :id;

-- name: StampTrackAnalyzed :exec
UPDATE tracks SET analyzed_at = :analyzed_at WHERE id = :id;

-- name: StampTrackReady :exec
UPDATE tracks SET ready_at = :ready_at WHERE id = :id;

-- name: GetTrackStatus :scalar
-- columns: status TEXT
SELECT status FROM tracks WHERE id = :id;

-- The ingestion-state endpoint and the startup resume both want progress
-- without dragging every analysis blob through the driver.
-- name: ListTrackStatuses :many
-- columns: id TEXT, name TEXT, artist TEXT, mixable BOOLEAN, status TEXT,
--          status_error TEXT, fetched_at REAL, analyzed_at REAL, ready_at REAL
SELECT id, name, artist, mixable, status, status_error,
       fetched_at, analyzed_at, ready_at
FROM tracks
ORDER BY id;

-- name: DeleteTrack :exec
DELETE FROM tracks WHERE id = :id;
