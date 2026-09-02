"""Flask API. Startup ingestion runs from config/tracks.json (see README).

Endpoints
  GET /api/health
  GET /api/tracks                          catalog + attribution + flags
  GET /api/tracks/<id>                     analysis summary + segments
  GET /api/tracks/<id>/waveform?bpm=&points=   downsampled energy envelope
  GET /api/tracks/<id>/audio?bpm=          variant WAV (or master if no bpm)
  GET /api/tracks/<id>/recommendations     Phase 2 ranked candidates
  GET /api/transitions?a=&b=               Phase 3 scored curve + markers
  GET /api/credits                         OSS license credits payload
  GET /api/latency                         latency measurements summary

Every handler opens its own read scope on the `Database` (see backend/db):
connections are per-thread and held only for the life of the request, rather
than one process-wide handle shared across Flask's worker threads.
"""
import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import config, ingest, licensing, matching, transitions
from .db import Database
from .db.catalog import grid_bpms_by_track
from .timing import Timer

FRONTEND_DIR = config.ROOT / "frontend"


def create_app(run_ingestion=True, database=None):
    app = Flask(__name__, static_folder=None)
    config.ensure_dirs()
    database = database or Database.from_config().migrate()
    app.config["DATABASE"] = database

    if run_ingestion:
        with database.reading() as q:
            empty = not q.count_tracks()
        if empty:
            print("[startup] ingesting catalog from", config.TRACKS_CONFIG)
            for r in ingest.ingest_all(database, Timer(database)):
                print("  ingested", r["id"], f"bpm={r['bpm']}", f"key={r['camelot']}",
                      ("variants=" + ",".join(map(str, r["grid_bpms"]))) if r["mixable"]
                      else "NOT MIXABLE (ND license)")

    # ---------- frontend ----------
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/css/<path:p>")
    def css(p):
        return send_from_directory(FRONTEND_DIR / "css", p)

    @app.get("/js/<path:p>")
    def js(p):
        return send_from_directory(FRONTEND_DIR / "js", p)

    # ---------- api ----------
    @app.get("/api/health")
    def health():
        with database.reading() as q:
            return jsonify({"ok": True, "tracks": q.count_tracks()})

    @app.get("/api/tracks")
    def tracks():
        with database.reading() as q:
            # Summaries omit the analysis/segment blobs, which this payload
            # does not use; grids come back in one query rather than per track.
            summaries = q.list_track_summaries()
            grids = grid_bpms_by_track(q)
        return jsonify([{
            "id": t.id, "name": t.name, "artist": t.artist,
            "genre": t.genre, "bpm": t.native_bpm, "camelot": t.camelot,
            "duration_s": t.duration_s, "mixable": t.mixable,
            "license_flags": {"nd": t.license_nd, "sa": t.license_sa,
                              "nc": t.license_nc},
            "attribution": licensing.attribution(t),
            "grid_bpms": grids.get(t.id, []),
        } for t in summaries])

    @app.get("/api/tracks/<tid>")
    def track_detail(tid):
        with database.reading() as q:
            t = q.get_track(id=tid)
        if not t:
            abort(404)
        a = t.analysis_json
        return jsonify({
            "id": t.id, "name": t.name, "artist": t.artist,
            "bpm": t.native_bpm, "camelot": t.camelot,
            "duration_s": t.duration_s, "mixable": t.mixable,
            "attribution": licensing.attribution(t),
            "segments": t.segments_json,
            "beat_grid": a["beat_grid"] if a else [],
        })

    @app.get("/api/tracks/<tid>/waveform")
    def waveform(tid):
        """Downsampled RMS envelope from cached analysis (P4-11: not
        recomputed client-side). ?bpm= rescales times to that grid variant."""
        with database.reading() as q:
            a = q.get_track_analysis(id=tid)
        if not a:
            abort(404)
        points = int(request.args.get("points", 300))
        bpm = request.args.get("bpm", type=float)
        ratio = (bpm / a["bpm"]) if bpm else 1.0

        rms = np.asarray(a["frames"]["rms"])
        idx = np.linspace(0, len(rms) - 1, points).astype(int)
        env = rms[idx]
        peak = env.max() or 1.0
        hop_dur = a["frames"]["hop_dur"] / ratio
        beat_grid = [b / ratio for b in a["beat_grid"]]
        return jsonify({
            "points": (env / peak).round(4).tolist(),
            "duration_s": a["duration_s"] / ratio,
            "hop_dur": hop_dur,
            "beat_grid": beat_grid,
            "bpm": a["bpm"] * ratio,
        })

    @app.get("/api/tracks/<tid>/audio")
    def audio(tid):
        bpm = request.args.get("bpm", type=int)
        with database.reading() as q:
            t = q.get_track(id=tid)
            if not t:
                abort(404)
            if bpm is None:
                return send_file(t.audio_path, mimetype="audio/wav")
            variants = q.list_variants_for_track(track_id=tid)
        for v in variants:
            if v.grid_bpm == bpm:
                return send_file(v.path, mimetype="audio/wav")
        abort(404, f"No variant at {bpm} BPM (track may be ND-restricted)")

    @app.get("/api/tracks/<tid>/recommendations")
    def recommendations(tid):
        timer = Timer(database)
        with timer.stage("recommend", tid):
            with database.reading() as q:
                recs = matching.recommend(q, tid)
        return jsonify(recs)

    @app.get("/api/transitions")
    def transitions_api():
        a_id, b_id = request.args.get("a"), request.args.get("b")
        with database.reading() as q:
            ta, tb = q.get_track(id=a_id), q.get_track(id=b_id)
            if not ta or not tb:
                abort(404)
            if not (ta.mixable and tb.mixable):
                abort(403, "ND-licensed tracks cannot be mixed (playback only)")
            grids = grid_bpms_by_track(q)

        m = matching.match(ta, tb, ta.analysis_json, ta.segments_json,
                           tb.analysis_json, tb.segments_json,
                           grids.get(a_id, []), grids.get(b_id, []))
        grid = m["best_grid_bpm"]
        if grid is None:
            abort(409, "Tracks share no BPM grid point")
        from .analysis import rescale_analysis
        an_a = rescale_analysis(ta.analysis_json, grid / ta.native_bpm)
        an_b = rescale_analysis(tb.analysis_json, grid / tb.native_bpm)
        timer = Timer(database)
        with timer.stage("transition_curve", f"{a_id}->{b_id}"):
            result = transitions.score_pair(an_a, ta.segments_json,
                                            an_b, tb.segments_json)
        result["grid_bpm"] = grid
        result["match"] = m
        return jsonify(result)

    @app.get("/api/credits")
    def credits():
        return jsonify(config.CREDITS)

    @app.get("/api/latency")
    def latency():
        with database.reading() as q:
            rows = q.latency_summary()
        return jsonify([{"stage": r.stage, "n": r.n, "mean_ms": r.mean_ms,
                         "min_ms": r.min_ms, "max_ms": r.max_ms} for r in rows])

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5050, debug=False)
