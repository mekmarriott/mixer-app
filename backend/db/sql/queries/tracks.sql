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
--          status TEXT, outro_energy REAL, intro_energy REAL
SELECT id, name, artist, genre, license,
       license_nd, license_sa, license_nc,
       mixable, native_bpm, camelot, duration_s, status,
       outro_energy, intro_energy
FROM tracks
ORDER BY id;

-- Everything a recommendation response needs, blob-free, in one statement:
-- the columns scoring reads and the envelope each deck row draws. Fetching the
-- envelopes separately would mean either a round trip per winner or a second
-- catalog-wide read; they are small enough to ride along with the scan that
-- has to happen anyway.
-- name: ListDeckRows :many
-- columns: id TEXT, name TEXT, artist TEXT, genre TEXT, license TEXT,
--          license_nd BOOLEAN, license_sa BOOLEAN, license_nc BOOLEAN,
--          mixable BOOLEAN, native_bpm REAL, camelot TEXT, duration_s REAL,
--          status TEXT, outro_energy REAL, intro_energy REAL,
--          deck_waveform JSONDOC
SELECT id, name, artist, genre, license,
       license_nd, license_sa, license_nc,
       mixable, native_bpm, camelot, duration_s, status,
       outro_energy, intro_energy, deck_waveform
FROM tracks
ORDER BY id;

-- The track being matched against, without dragging its blobs along.
-- name: GetTrackSummary :one
-- columns: id TEXT, name TEXT, artist TEXT, genre TEXT, license TEXT,
--          license_nd BOOLEAN, license_sa BOOLEAN, license_nc BOOLEAN,
--          mixable BOOLEAN, native_bpm REAL, camelot TEXT, duration_s REAL,
--          status TEXT, outro_energy REAL, intro_energy REAL
SELECT id, name, artist, genre, license,
       license_nd, license_sa, license_nc,
       mixable, native_bpm, camelot, duration_s, status,
       outro_energy, intro_energy
FROM tracks WHERE id = :id;

-- name: GetTrackNativeEnvelope :scalar
-- columns: native_envelope JSONDOC
SELECT native_envelope FROM tracks WHERE id = :id;

-- name: SetTrackNativeEnvelope :exec
UPDATE tracks SET native_envelope = :native_envelope WHERE id = :id;

-- name: ListTracksMissingNativeEnvelope :many
-- columns: id TEXT
SELECT id FROM tracks
WHERE analysis_json IS NOT NULL AND native_envelope IS NULL
ORDER BY id;

-- name: SetTrackDeckWaveform :exec
UPDATE tracks SET deck_waveform = :deck_waveform WHERE id = :id;

-- name: ListTracksMissingDeckWaveform :many
-- columns: id TEXT
SELECT id FROM tracks
WHERE analysis_json IS NOT NULL AND deck_waveform IS NULL
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
                    audio_key, analysis_json, segments_json,
                    status, status_error, source_url,
                    fetched_at, analyzed_at, ready_at)
VALUES (:id, :name, :artist, :genre, :license, :license_nd, :license_sa,
        :license_nc, :mixable, :native_bpm, :camelot, :duration_s,
        :audio_key, :analysis_json, :segments_json,
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
    audio_key    = COALESCE(EXCLUDED.audio_key, tracks.audio_key),
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
UPDATE tracks SET analysis_json = NULL, segments_json = NULL,
                 outro_energy = NULL, intro_energy = NULL,
                 deck_waveform = NULL, native_envelope = NULL
WHERE id = :id;

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

-- Backfill support. A row written before the energy columns existed, or by an
-- analysis they could not be derived from, still has the blobs to derive them
-- from; matching falls back to reading those blobs, which is exactly the cost
-- the columns exist to avoid, so the gap is worth closing once.
-- name: ListTracksMissingEnergies :many
-- columns: id TEXT
SELECT id FROM tracks
WHERE analysis_json IS NOT NULL
  AND segments_json IS NOT NULL
  AND (outro_energy IS NULL OR intro_energy IS NULL)
ORDER BY id;

-- name: SetTrackEnergies :exec
UPDATE tracks SET outro_energy = :outro_energy, intro_energy = :intro_energy
WHERE id = :id;

-- name: DeleteTrack :exec
DELETE FROM tracks WHERE id = :id;
