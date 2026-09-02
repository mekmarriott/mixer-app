-- Saved mixes and their track chains. See tracks.sql for the annotation format.

-- name: ListMixes :many
SELECT * FROM mixes ORDER BY updated_at DESC;

-- name: GetMix :one
SELECT * FROM mixes WHERE id = :id;

-- name: CountMixes :scalar
-- columns: n INTEGER
SELECT COUNT(*) AS n FROM mixes;

-- name: UpsertMix :exec
INSERT INTO mixes (id, name, head_id, created_at, updated_at)
VALUES (:id, :name, :head_id, :created_at, :updated_at)
ON CONFLICT (id) DO UPDATE SET
    name       = EXCLUDED.name,
    head_id    = EXCLUDED.head_id,
    updated_at = EXCLUDED.updated_at;

-- Rename and re-stamp without touching the chain.
-- name: RenameMix :exec
UPDATE mixes SET name = :name, updated_at = :updated_at WHERE id = :id;

-- name: SetMixHead :exec
UPDATE mixes SET head_id = :head_id, updated_at = :updated_at WHERE id = :id;

-- name: TouchMix :exec
UPDATE mixes SET updated_at = :updated_at WHERE id = :id;

-- name: DeleteMix :exec
DELETE FROM mixes WHERE id = :id;

-- The whole chain in one read. Order is meaningless here — the caller walks
-- next_id — so this is a set fetch, not a sequence.
-- name: ListMixTracks :many
SELECT * FROM mix_tracks WHERE mix_id = :mix_id;

-- Track counts for the whole picker in ONE query. Listing mixes previously
-- issued a chain read per mix, so the cost of opening the menu grew with the
-- number of saved mixes — and the menu is refreshed after every save.
-- name: CountMixTracksByMix :many
-- columns: mix_id TEXT, n INTEGER
SELECT mix_id, COUNT(*) AS n FROM mix_tracks GROUP BY mix_id;

-- name: GetMixTrack :one
SELECT * FROM mix_tracks WHERE id = :id;

-- name: UpsertMixTrack :exec
INSERT INTO mix_tracks (id, mix_id, track_id, next_id, delta_beats, grid_bpm)
VALUES (:id, :mix_id, :track_id, :next_id, :delta_beats, :grid_bpm)
ON CONFLICT (id) DO UPDATE SET
    track_id    = EXCLUDED.track_id,
    next_id     = EXCLUDED.next_id,
    delta_beats = EXCLUDED.delta_beats,
    grid_bpm    = EXCLUDED.grid_bpm;

-- The ripple edit: one row, one column. This is the write a track drag makes.
-- name: SetMixTrackDelta :exec
UPDATE mix_tracks SET delta_beats = :delta_beats WHERE id = :id;

-- name: SetMixTrackNext :exec
UPDATE mix_tracks SET next_id = :next_id WHERE id = :id;

-- name: DeleteMixTrack :exec
DELETE FROM mix_tracks WHERE id = :id;

-- name: DeleteMixTracksForMix :exec
DELETE FROM mix_tracks WHERE mix_id = :mix_id;
