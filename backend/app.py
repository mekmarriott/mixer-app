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
"""
import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import config, db, ingest, licensing, matching, transitions
from .timing import Timer

FRONTEND_DIR = config.ROOT / "frontend"


def create_app(run_ingestion=True):
    app = Flask(__name__, static_folder=None)
    config.ensure_dirs()
    con = db.connect()
    app.config["DB"] = con

    if run_ingestion and not db.all_tracks(con):
        print("[startup] ingesting catalog from", config.TRACKS_CONFIG)
        for r in ingest.ingest_all(con, Timer(con)):
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
        return jsonify({"ok": True, "tracks": len(db.all_tracks(con))})

    @app.get("/api/tracks")
    def tracks():
        out = []
        for t in db.all_tracks(con):
            out.append({
                "id": t["id"], "name": t["name"], "artist": t["artist"],
                "genre": t["genre"], "bpm": t["native_bpm"], "camelot": t["camelot"],
                "duration_s": t["duration_s"], "mixable": bool(t["mixable"]),
                "license_flags": {"nd": bool(t["license_nd"]),
                                  "sa": bool(t["license_sa"]),
                                  "nc": bool(t["license_nc"])},
                "attribution": licensing.attribution(t),
                "grid_bpms": [v["grid_bpm"] for v in db.variants_for(con, t["id"])],
            })
        return jsonify(out)

    @app.get("/api/tracks/<tid>")
    def track_detail(tid):
        t = db.get_track(con, tid)
        if not t:
            abort(404)
        a = db.analysis_of(con, tid)
        return jsonify({
            "id": t["id"], "name": t["name"], "artist": t["artist"],
            "bpm": t["native_bpm"], "camelot": t["camelot"],
            "duration_s": t["duration_s"], "mixable": bool(t["mixable"]),
            "attribution": licensing.attribution(t),
            "segments": db.segments_of(con, tid),
            "beat_grid": a["beat_grid"] if a else [],
        })

    @app.get("/api/tracks/<tid>/waveform")
    def waveform(tid):
        """Downsampled RMS envelope from cached analysis (P4-11: not
        recomputed client-side). ?bpm= rescales times to that grid variant."""
        t = db.get_track(con, tid)
        a = db.analysis_of(con, tid)
        if not t or not a:
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
        t = db.get_track(con, tid)
        if not t:
            abort(404)
        bpm = request.args.get("bpm", type=int)
        if bpm is None:
            return send_file(t["audio_path"], mimetype="audio/wav")
        for v in db.variants_for(con, tid):
            if v["grid_bpm"] == bpm:
                return send_file(v["path"], mimetype="audio/wav")
        abort(404, f"No variant at {bpm} BPM (track may be ND-restricted)")

    @app.get("/api/tracks/<tid>/recommendations")
    def recommendations(tid):
        timer = Timer(con)
        with timer.stage("recommend", tid):
            recs = matching.recommend(con, tid)
        return jsonify(recs)

    @app.get("/api/transitions")
    def transitions_api():
        a_id, b_id = request.args.get("a"), request.args.get("b")
        ta, tb = db.get_track(con, a_id), db.get_track(con, b_id)
        if not ta or not tb:
            abort(404)
        if not (ta["mixable"] and tb["mixable"]):
            abort(403, "ND-licensed tracks cannot be mixed (playback only)")
        m = matching.match(ta, tb, db.analysis_of(con, a_id), db.segments_of(con, a_id),
                           db.analysis_of(con, b_id), db.segments_of(con, b_id),
                           db.variants_for(con, a_id), db.variants_for(con, b_id))
        grid = m["best_grid_bpm"]
        if grid is None:
            abort(409, "Tracks share no BPM grid point")
        from .analysis import rescale_analysis
        an_a = rescale_analysis(db.analysis_of(con, a_id), grid / ta["native_bpm"])
        an_b = rescale_analysis(db.analysis_of(con, b_id), grid / tb["native_bpm"])
        timer = Timer(con)
        with timer.stage("transition_curve", f"{a_id}->{b_id}"):
            result = transitions.score_pair(an_a, db.segments_of(con, a_id),
                                            an_b, db.segments_of(con, b_id))
        result["grid_bpm"] = grid
        result["match"] = m
        return jsonify(result)

    @app.get("/api/credits")
    def credits():
        return jsonify(config.CREDITS)

    @app.get("/api/latency")
    def latency():
        return jsonify(db.latency_summary(con))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5050, debug=False)
