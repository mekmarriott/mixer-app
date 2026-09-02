"""Flask API. Startup ingestion + waveform precompute run from
config/tracks.json (see README).

Endpoints
  GET /api/status                          warmup phase/progress + admission stats
  GET /api/health                          liveness (never gated)
  GET /api/ingest                          per-track ingestion state (never gated)
  GET /api/deck                            zero-state: genres x N, waveforms inline
  GET    /api/mixes                        saved mixes, most recently edited first
  POST   /api/mixes                        create an (empty) mix
  GET    /api/mixes/<id>                   one mix with its ordered track chain
  PATCH  /api/mixes/<id>                   rename
  DELETE /api/mixes/<id>                   delete a mix and its chain
  PUT    /api/mixes/<id>/tracks            replace the chain (structural edits)
  PATCH  /api/mixes/<id>/tracks/<node>     move ONE track (the drag write)
  GET /api/tracks                          catalog + attribution + flags
  GET /api/tracks/<id>                     analysis summary + segments
  GET /api/tracks/<id>/waveform?bpm=&points=   cached envelope
  GET /api/tracks/<id>/audio?bpm=          variant WAV (or master if no bpm)
  GET /api/tracks/<id>/recommendations     Phase 2 ranked candidates
  GET /api/transitions?a=&b=               Phase 3 scored curve + markers
  GET /api/credits                         OSS license credits payload
  GET /api/latency                         latency measurements summary

Every handler opens its own read scope on the `Database` (see backend/db):
connections are per-thread and held only for the life of the request, rather
than one process-wide handle shared across Flask's worker threads. That scope
is wrapped by `backend/dbguard.BoundedDatabase`, which caps how many requests
may be inside the database at once — see its module docstring for why
per-thread connections alone do not bound concurrency.

Handlers that need catalog data are gated on warmup, so a browser arriving
mid-startup gets a 503 with progress it can render as a wait screen instead of
a partial page.
"""
from functools import wraps

from flask import (Flask, abort, jsonify, redirect, request, send_file,
                   send_from_directory)

from . import (config, dbguard, licensing, matching, mixes as mixes_mod,
               storage, transitions)
from . import warmup as warmup_mod
from . import waveforms
from .db import Database
from .db import status as track_status
from .db.catalog import grid_bpms_by_track
from .timing import Timer

FRONTEND_DIR = config.ROOT / "frontend"


def create_app(run_ingestion=True, database=None, warmup_async=False):
    """warmup_async=False blocks until the catalog is ready (tests, CLI);
    True binds the port immediately and warms in the background (dev server)."""
    app = Flask(__name__, static_folder=None)
    store = storage.get_store()
    # Only the local backend has a data directory to create. Under a remote
    # store the filesystem is read-only (Vercel) and nothing here may write.
    if isinstance(store, storage.LocalBlobStore):
        config.ensure_dirs()

    base = database or Database.from_config().migrate()
    # Admission control sits above the engine so it applies to SQLite (which
    # otherwise has no ceiling: one connection per thread, unbounded threads)
    # and to Postgres alike.
    database = base if isinstance(base, dbguard.BoundedDatabase) \
        else dbguard.BoundedDatabase(base)

    repo = mixes_mod.MixRepository(database)
    cache = waveforms.WaveformCache()
    warm = warmup_mod.Warmup(database, cache)
    app.config.update(DATABASE=database, WAVEFORMS=cache, WARMUP=warm,
                      BLOB_STORE=store, MIXES=repo)

    if warmup_async:
        warm.start_async(run_ingestion=run_ingestion)
    else:
        warm.run(run_ingestion=run_ingestion)

    # ------------------------------------------------------------ plumbing
    @app.errorhandler(dbguard.AdmissionTimeout)
    def _saturated(exc):  # pragma: no cover - saturation only
        resp = jsonify({"error": "db_busy", "detail": str(exc),
                        "db": database.snapshot()})
        resp.status_code = 503
        resp.headers["Retry-After"] = "1"
        return resp

    def needs_catalog(fn):
        """Gate a handler on warmup. 503 + Retry-After is the correct answer to
        'the data does not exist yet' — the client renders a wait screen from
        the same payload /api/status returns."""
        @wraps(fn)
        def wrapper(*a, **kw):
            if not warm.ready:
                snap = warm.snapshot()
                resp = jsonify({"error": "warming_up", "status": snap})
                resp.status_code = 500 if snap["phase"] == warmup_mod.FAILED else 503
                resp.headers["Retry-After"] = "1"
                return resp
            return fn(*a, **kw)
        return wrapper

    def track_durations():
        """`(track_id, grid_bpm) -> seconds` for the overlap check.

        The RENDERED VARIANT's duration, not the track's native one. A track
        stretched onto a different grid genuinely plays for a different length
        — up to 5s on the test catalog — and the client draws, plays and clamps
        against the variant it loaded. Feeding native durations to
        check_overlaps made the server disagree with the client about where a
        track ends, so a placement the client had correctly clamped came back
        409 and the mix silently failed to save.

        Native remains the fallback for a track with no variant at that grid
        (an ND track, or a grid point never rendered), which is the only case
        where the two cannot differ anyway.
        """
        with database.reading() as q:
            native = {t.id: (t.duration_s or 0.0) for t in q.list_track_summaries()}
            rendered = {(v.track_id, int(v.grid_bpm)): (v.duration_s or 0.0)
                        for v in q.list_all_variants()}

        def duration_of(track_id, grid_bpm=None):
            if grid_bpm is not None:
                hit = rendered.get((track_id, int(grid_bpm)))
                if hit:
                    return hit
            return native.get(track_id, 0.0)

        return duration_of

    def mix_error(exc, code=409):
        resp = jsonify({"error": "invalid_chain", "detail": str(exc)})
        resp.status_code = code
        return resp

    def track_payload(t, grids=None, wf_points=None, analysis_loader=None):
        """`t` is a db.Track or the blob-free db.ListTrackSummariesRow.

        `analysis_loader` supplies the analysis for a waveform the warmup pass
        did not precompute. Warmup only warms the deck now, so a row outside it
        — every recommendation past the opening screen — would otherwise carry
        `waveform: null`. The loader is passed in rather than opened here
        because callers already hold a read scope, and taking a second
        connection from inside one competes with the admission gate for a pool
        that is only `connection_ceiling` deep.
        """
        row = {
            "id": t.id, "name": t.name, "artist": t.artist, "genre": t.genre,
            "bpm": t.native_bpm, "camelot": t.camelot,
            "duration_s": t.duration_s, "mixable": t.mixable,
            "license_flags": {"nd": t.license_nd, "sa": t.license_sa,
                              "nc": t.license_nc},
            "attribution": licensing.attribution(t),
        }
        if grids is not None:
            row["grid_bpms"] = grids.get(t.id, [])
        if wf_points:
            # Inline the thumbnail so a deck row costs zero extra requests.
            if analysis_loader is None:
                wf = cache.get(t.id, wf_points)
            else:
                wf = cache.get_or_compute(t.id, wf_points, None,
                                          lambda: analysis_loader(t.id))
            row["waveform"] = wf["points"] if wf else None
        return row

    # ------------------------------------------------------------ frontend
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/css/<path:p>")
    def css(p):
        return send_from_directory(FRONTEND_DIR / "css", p)

    @app.get("/js/<path:p>")
    def js(p):
        return send_from_directory(FRONTEND_DIR / "js", p)

    # ----------------------------------------------------------- readiness
    @app.get("/api/status")
    def status():
        """Never gated — this is what the wait screen polls."""
        return jsonify({**warm.snapshot(), "db": database.snapshot()})

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "ready": warm.ready})

    @app.get("/api/ingest")
    def ingest_status():
        """Per-track ingestion state. Not gated on warmup: this is precisely
        what you want to read while the catalog is still being built, and
        after a run that left some tracks incomplete.

        `ready` means audio fetched, analysis cached and variants rendered.
        Anything else is still in progress; `failed` carries the reason the
        last attempt stopped, and the stage it stopped at is where the next
        run resumes from.
        """
        cfg = config.load_tracks_config()
        state = database.catalog.ingestion_state([t["id"] for t in cfg["tracks"]])
        counts = {}
        for entry in state:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        # `complete` is about the seed file only: a track published straight
        # into the database was never ingestion's job, so it cannot make
        # ingestion incomplete — but it is still listed, flagged in_config
        # false, so it is visible rather than silently absent.
        seeded = [e for e in state if e["in_config"]]
        return jsonify({
            "mode": cfg["mode"], "counts": counts,
            "configured": len(seeded),
            "unconfigured": len(state) - len(seeded),
            "failed": sum(1 for entry in state if entry["failed"]),
            "complete": all(e["status"] == track_status.READY for e in seeded),
            "tracks": state})

    # ----------------------------------------------------------- zero state
    @app.get("/api/deck")
    @needs_catalog
    def deck_view():
        """Opening view: a few tracks per genre, waveforms inline.

        No matching and no pair analysis happen here — with nothing selected
        there is nothing to score against. Served entirely from the warmup
        snapshot, so it touches the database not at all.
        """
        return jsonify({"groups": warm.deck,
                        "per_genre": config.DECK_TRACKS_PER_GENRE})

    # -------------------------------------------------------------- mixes
    @app.get("/api/mixes")
    @needs_catalog
    def list_mixes():
        return jsonify(repo.list())

    @app.post("/api/mixes")
    @needs_catalog
    def create_mix():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "Untitled Mix").strip() or "Untitled Mix"
        return jsonify(repo.create(name=name)), 201

    @app.get("/api/mixes/<mid>")
    @needs_catalog
    def get_mix(mid):
        try:
            mix = repo.get(mid, track_durations())
        except mixes_mod.ChainError as exc:
            return mix_error(exc, 500)      # stored data is corrupt, not the request
        if mix is None:
            abort(404)
        return jsonify(mix)

    @app.patch("/api/mixes/<mid>")
    @needs_catalog
    def rename_mix(mid):
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            abort(400, "name is required")
        repo.rename(mid, name)
        return jsonify({"id": mid, "name": name})

    @app.delete("/api/mixes/<mid>")
    @needs_catalog
    def delete_mix(mid):
        repo.delete(mid)
        return "", 204

    @app.put("/api/mixes/<mid>/tracks")
    @needs_catalog
    def replace_mix_tracks(mid):
        """Structural edit: append, insert, delete or reorder. The client sends
        the resulting chain; the server validates and writes it atomically."""
        body = request.get_json(silent=True) or {}
        try:
            repo.replace_chain(mid, body.get("tracks") or [], track_durations())
        except mixes_mod.ChainError as exc:
            return mix_error(exc)
        return jsonify(repo.get(mid, track_durations()))

    @app.patch("/api/mixes/<mid>/tracks/<node>")
    @needs_catalog
    def move_mix_track(mid, node):
        """The drag write: one row, one column. Cheap by construction, which is
        why a drag can afford to persist at all."""
        body = request.get_json(silent=True) or {}
        if "delta_beats" not in body:
            abort(400, "delta_beats is required")
        try:
            entry = repo.set_delta(mid, node, int(body["delta_beats"]),
                                   track_durations())
        except mixes_mod.ChainError as exc:
            return mix_error(exc)
        if entry is None:
            abort(404)
        return jsonify(entry)

    # ---------------------------------------------------------------- api
    @app.get("/api/tracks")
    @needs_catalog
    def tracks():
        with database.reading() as q:
            # Summaries omit the analysis/segment blobs, which this payload
            # does not use; grids come back in one query rather than per track.
            summaries = [t for t in q.list_track_summaries()
                         if t.status == track_status.READY]   # never show a partial
            grids = grid_bpms_by_track(q)
        return jsonify([track_payload(t, grids) for t in summaries])

    @app.get("/api/tracks/<tid>")
    @needs_catalog
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
    @needs_catalog
    def waveform(tid):
        """Served from the warmup cache. Native envelopes are always warm;
        only grid variants (needed after a pair is chosen) can miss, and that
        miss goes through the same admission gate as everything else."""
        points = int(request.args.get("points", config.TIMELINE_WAVEFORM_POINTS))
        bpm = request.args.get("bpm", type=float)

        hit = cache.get(tid, points, bpm)
        if hit is not None:
            return jsonify(hit)

        def load():
            with database.reading() as q:
                return q.get_track_analysis(id=tid)

        result = cache.get_or_compute(tid, points, bpm, load)
        if result is None:
            abort(404)
        return jsonify(result)

    @app.get("/api/tracks/<tid>/audio")
    @needs_catalog
    def audio(tid):
        """Redirect to the object store; never proxy the bytes.

        Streaming audio through this handler would bill the transfer to the API
        and hold a serverless function open for the whole download. It would
        also hold a database admission permit for the duration of a multi-
        megabyte transfer, which is exactly the stall dbguard exists to
        prevent. The 302 sends the client straight to the CDN instead
        (docs/infrastructure-plan.md §1.2).
        """
        bpm = request.args.get("bpm", type=int)
        with database.reading() as q:
            t = q.get_track(id=tid)
            if not t:
                abort(404)
            if bpm is None:
                return redirect(store.url_for(t.audio_key), code=302)
            variants = q.list_variants_for_track(track_id=tid)
        for v in variants:
            if v.grid_bpm == bpm:
                return redirect(store.url_for(v.object_key), code=302)
        abort(404, f"No variant at {bpm} BPM (track may be ND-restricted)")

    @app.get("/blobs/<path:key>")
    def blob(key):
        """Serve the local blob backend. Development and tests only — in
        deployment the 302 above points at Vercel Blob/R2 and this route is
        never reached. Deliberately not @needs_catalog and not DB-touching:
        it is a static file read."""
        try:
            path = store.local_path(key)
        except storage.BlobStoreError:
            # Traversal attempt. The store refuses it; this is the only place
            # a key arrives from a request rather than from ingestion, so it
            # must answer 404 rather than leak the refusal as a 500.
            abort(404)
        if path is None or not path.is_file():
            abort(404)
        return send_file(path, mimetype="audio/wav", conditional=True)

    @app.get("/api/tracks/<tid>/recommendations")
    @needs_catalog
    def recommendations(tid):
        """Phase 2 ranking — the first computation that depends on a choice.
        Waveforms are inlined so the suggestion deck, like the zero-state deck,
        needs no follow-up requests."""
        timer = Timer(database)
        with timer.stage("recommend", tid):
            with database.reading() as q:
                recs = matching.recommend(q, tid)
                for r in recs:
                    t = q.get_track(id=r["track_id"])
                    if t:
                        # get_track already carried the analysis blob back, so
                        # the waveform for a candidate outside the warmed deck
                        # costs no further query.
                        r["track"] = track_payload(
                            t, wf_points=config.DECK_WAVEFORM_POINTS,
                            analysis_loader=lambda _id, t=t: t.analysis_json)
        return jsonify(recs)

    @app.get("/api/transitions")
    @needs_catalog
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
    @needs_catalog
    def latency():
        with database.reading() as q:
            rows = q.latency_summary()
        return jsonify([{"stage": r.stage, "n": r.n, "mean_ms": r.mean_ms,
                         "min_ms": r.min_ms, "max_ms": r.max_ms} for r in rows])

    return app


if __name__ == "__main__":
    # Bind immediately and warm in the background so the browser can show a
    # progress screen rather than hanging on a dead socket.
    create_app(warmup_async=True).run(host="127.0.0.1", port=5050, debug=False,
                                      threaded=True)
