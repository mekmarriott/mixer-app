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
--          mixable BOOLEAN, native_bpm REAL, camelot TEXT, duration_s REAL
SELECT id, name, artist, genre, license,
       license_nd, license_sa, license_nc,
       mixable, native_bpm, camelot, duration_s
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
                    audio_path, analysis_json, segments_json)
VALUES (:id, :name, :artist, :genre, :license, :license_nd, :license_sa,
        :license_nc, :mixable, :native_bpm, :camelot, :duration_s,
        :audio_path, :analysis_json, :segments_json)
ON CONFLICT (id) DO UPDATE SET
    name          = EXCLUDED.name,
    artist        = EXCLUDED.artist,
    genre         = EXCLUDED.genre,
    license       = EXCLUDED.license,
    license_nd    = EXCLUDED.license_nd,
    license_sa    = EXCLUDED.license_sa,
    license_nc    = EXCLUDED.license_nc,
    mixable       = EXCLUDED.mixable,
    native_bpm    = EXCLUDED.native_bpm,
    camelot       = EXCLUDED.camelot,
    duration_s    = EXCLUDED.duration_s,
    audio_path    = EXCLUDED.audio_path,
    analysis_json = EXCLUDED.analysis_json,
    segments_json = EXCLUDED.segments_json;

-- name: DeleteTrack :exec
DELETE FROM tracks WHERE id = :id;
