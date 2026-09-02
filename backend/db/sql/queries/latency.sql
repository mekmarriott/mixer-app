-- Latency instrumentation queries. See tracks.sql for the annotation format.

-- name: InsertLatency :exec
INSERT INTO latency (stage, track_id, ms, at)
VALUES (:stage, :track_id, :ms, :at);

-- name: LatencySummary :many
-- columns: stage TEXT, n INTEGER, mean_ms REAL, min_ms REAL, max_ms REAL
SELECT stage,
       COUNT(*) AS n,
       AVG(ms)  AS mean_ms,
       MIN(ms)  AS min_ms,
       MAX(ms)  AS max_ms
FROM latency
GROUP BY stage
ORDER BY stage;
