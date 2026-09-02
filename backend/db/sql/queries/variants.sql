-- Rendered tempo-variant queries. See tracks.sql for the annotation format.

-- name: ListVariantsForTrack :many
SELECT * FROM variants WHERE track_id = :track_id ORDER BY grid_bpm;

-- Whole-catalog fetch, grouped by track in Python. The catalog endpoint and the
-- recommendation scan both need every track's grid; issuing one query per track
-- made those O(catalog) round trips.
-- name: ListAllVariants :many
SELECT * FROM variants ORDER BY track_id, grid_bpm;

-- name: UpsertVariant :exec
INSERT INTO variants (track_id, grid_bpm, ratio, object_key, duration_s)
VALUES (:track_id, :grid_bpm, :ratio, :object_key, :duration_s)
ON CONFLICT (track_id, grid_bpm) DO UPDATE SET
    ratio      = EXCLUDED.ratio,
    object_key = EXCLUDED.object_key,
    duration_s = EXCLUDED.duration_s;

-- name: DeleteVariantsForTrack :exec
DELETE FROM variants WHERE track_id = :track_id;
