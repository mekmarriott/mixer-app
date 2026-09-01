"""SQLite catalog store. JSON blobs for analysis; swap for Postgres/Supabase
in deployment (see design doc §Persistence)."""
import json
import sqlite3
import threading

from . import config

WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    genre TEXT NOT NULL,
    license TEXT NOT NULL,
    license_nd INTEGER NOT NULL,
    license_sa INTEGER NOT NULL,
    license_nc INTEGER NOT NULL,
    mixable INTEGER NOT NULL,           -- 0 for ND tracks (playback only)
    native_bpm REAL,
    camelot TEXT,
    duration_s REAL,
    audio_path TEXT,
    analysis_json TEXT,
    segments_json TEXT
);
CREATE TABLE IF NOT EXISTS variants (
    track_id TEXT NOT NULL,
    grid_bpm INTEGER NOT NULL,
    ratio REAL NOT NULL,
    path TEXT NOT NULL,
    duration_s REAL NOT NULL,
    PRIMARY KEY (track_id, grid_bpm)
);
CREATE TABLE IF NOT EXISTS latency (
    stage TEXT NOT NULL,
    track_id TEXT,
    ms REAL NOT NULL,
    at REAL NOT NULL
);
"""


def connect(path=None):
    # check_same_thread=False: Flask serves from worker threads; writes are
    # serialized behind WRITE_LOCK (SQLite handles its own file locking).
    con = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_track(con, row):
    con.execute(
        """INSERT OR REPLACE INTO tracks
           (id,name,artist,genre,license,license_nd,license_sa,license_nc,
            mixable,native_bpm,camelot,duration_s,audio_path,analysis_json,segments_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["id"], row["name"], row["artist"], row["genre"], row["license"],
         int(row["nd"]), int(row["sa"]), int(row["nc"]), int(row["mixable"]),
         row.get("native_bpm"), row.get("camelot"), row.get("duration_s"),
         row.get("audio_path"),
         json.dumps(row.get("analysis")) if row.get("analysis") else None,
         json.dumps(row.get("segments")) if row.get("segments") else None))
    con.commit()


def add_variant(con, track_id, grid_bpm, ratio, path, duration_s):
    con.execute("INSERT OR REPLACE INTO variants VALUES (?,?,?,?,?)",
                (track_id, int(grid_bpm), float(ratio), str(path), float(duration_s)))
    con.commit()


def get_track(con, track_id):
    r = con.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    return dict(r) if r else None


def all_tracks(con):
    return [dict(r) for r in con.execute("SELECT * FROM tracks ORDER BY id")]


def variants_for(con, track_id):
    return [dict(r) for r in con.execute(
        "SELECT * FROM variants WHERE track_id=? ORDER BY grid_bpm", (track_id,))]


def analysis_of(con, track_id):
    r = con.execute("SELECT analysis_json FROM tracks WHERE id=?", (track_id,)).fetchone()
    return json.loads(r["analysis_json"]) if r and r["analysis_json"] else None


def segments_of(con, track_id):
    r = con.execute("SELECT segments_json FROM tracks WHERE id=?", (track_id,)).fetchone()
    return json.loads(r["segments_json"]) if r and r["segments_json"] else None


def record_latency(con, stage, track_id, ms, at):
    with WRITE_LOCK:
        con.execute("INSERT INTO latency VALUES (?,?,?,?)", (stage, track_id, ms, at))
        con.commit()


def latency_summary(con):
    rows = con.execute(
        """SELECT stage, COUNT(*) n, AVG(ms) mean_ms, MIN(ms) min_ms, MAX(ms) max_ms
           FROM latency GROUP BY stage ORDER BY stage""").fetchall()
    return [dict(r) for r in rows]
