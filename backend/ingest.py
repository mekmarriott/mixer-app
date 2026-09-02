"""Ingestion pipeline (Phase 1). Orchestrates:

  fetch -> license gate -> persist master -> analyze (once, native tempo)
  -> segment -> plan BPM grid -> render variants (ND excluded) -> cache

Audio rendering happens outside the database transaction: variants are written
to disk first and committed together with the track's `ready` status in a
single write at the end, so a crash mid-render cannot leave a track in the
catalog advertising variants that were never recorded.

The pipeline is **resumable and idempotent**. Each track carries a status
high-water mark (pending -> fetched -> analyzed -> ready) and every stage is
skipped when its output is already durably on disk. Network fetches in
particular are never repeated for a track whose master WAV is present: a
rerun after a crash, an interrupted first start, or an added catalog entry
costs only the work that has not been done yet. Progress is written after
fetch and again after analysis so that an interruption keeps what it earned;
the track is not advertised by /api/tracks until it reaches `ready`.

Each stage is latency-instrumented (docs/latency-report.md is generated from
these measurements by backend/benchmark.py).
"""
from pathlib import Path

from . import analysis as analysis_mod
from . import bpm_grid, config, db, jamendo, licensing, segmentation, stretch
from .audio_io import load_wav, save_wav, wav_duration
from .timing import Timer

status = db.status


def _master_path(track_id):
    return config.AUDIO_DIR / f"{track_id}.wav"


def _variant_path(track_id, grid_bpm):
    return config.VARIANT_DIR / f"{track_id}_{grid_bpm}.wav"


def _usable(path):
    path = Path(path)
    return path.exists() and path.stat().st_size > 44      # bigger than a WAV header


def ingest_track(database, entry, mode, timer=None, force=False):
    """Ingest one track, resuming from whatever is already done.

    `force=True` re-runs every stage regardless of recorded state.
    """
    timer = timer or Timer(database)
    catalog = database.catalog
    tid = str(entry["id"])
    reused = []

    with database.reading() as q:
        existing = q.get_track(id=tid)

    try:
        # ---------------------------------------------------- fetch (network)
        have_master = (not force and existing is not None
                       and status.at_least(existing.status, status.FETCHED)
                       and _usable(existing.audio_path or _master_path(tid)))
        if have_master:
            with timer.stage("reuse_master", tid):
                samples, sr = load_wav(existing.audio_path or _master_path(tid))
            meta = {"id": tid, "name": existing.name, "artist": existing.artist,
                    "genre": existing.genre, "license": existing.license,
                    "audiodownload_allowed": True,
                    "source_url": existing.source_url or ""}
            lic = licensing.parse_license(meta["license"])
            reused.append("fetch")
        else:
            with timer.stage("fetch", tid):
                meta, samples, sr = jamendo.fetch_track(entry, mode)
            # Unknown licenses raise here, before anything is persisted (P1-07).
            lic = licensing.parse_license(meta["license"])
            with timer.stage("persist_master", tid):
                audio_path = jamendo.persist_master(meta, samples, sr)
            catalog.save_ingested_track({
                **meta, **lic,
                "mixable": not lic["nd"],
                "audio_path": str(audio_path),
                "duration_s": len(samples) / sr,
                "status": status.FETCHED,
            })
            catalog.advance_status(tid, status.FETCHED)

        mixable = not lic["nd"]                    # ND => playback only (P1-08)

        # --------------------------------------------------------- analysis
        have_analysis = (not force and existing is not None
                         and status.at_least(existing.status, status.ANALYZED)
                         and existing.analysis_json and existing.segments_json)
        if have_analysis:
            a, segs = existing.analysis_json, existing.segments_json
            reused.append("analysis")
        else:
            with timer.stage("analyze", tid):
                a = analysis_mod.analyze(samples, sr)
            with timer.stage("segment", tid):
                segs = segmentation.segment(a)
            catalog.save_ingested_track({
                **meta, **lic,
                "mixable": mixable,
                "native_bpm": a["bpm"],
                "camelot": a["key"]["camelot"],
                "duration_s": a["duration_s"],
                "audio_path": str(_master_path(tid)),
                "analysis": a,
                "segments": segs,
                "status": status.ANALYZED,
            })
            catalog.advance_status(tid, status.ANALYZED)

        # --------------------------------------------------------- variants
        grid, variants, rendered = [], [], 0
        if mixable:
            grid = bpm_grid.grid_points(a["bpm"], meta["genre"])
            to_render = []
            for g in grid:
                vpath = _variant_path(tid, g)
                if not force and _usable(vpath):
                    # Already on disk from an interrupted run: re-register it
                    # rather than paying for the stretch again.
                    variants.append((g, g / a["bpm"], str(vpath), wav_duration(vpath)))
                else:
                    to_render.append(g)
            if to_render:
                with timer.stage("render_variants", tid):
                    for g in to_render:
                        ratio = g / a["bpm"]
                        out = stretch.stretch(samples, sr, ratio)
                        vpath = _variant_path(tid, g)
                        save_wav(vpath, out, sr)
                        variants.append((g, ratio, str(vpath), len(out) / sr))
                        rendered += 1
            variants.sort()
            if len(variants) > rendered:
                reused.append(f"{len(variants) - rendered} variants")

        # Variants and `ready` land in one write: the catalog never advertises
        # a track whose variant rows are missing.
        catalog.save_ingested_track({
            **meta, **lic,
            "mixable": mixable,
            "native_bpm": a["bpm"],
            "camelot": a["key"]["camelot"],
            "duration_s": a["duration_s"],
            "audio_path": str(_master_path(tid)),
            "analysis": a,
            "segments": segs,
            "status": status.READY,
        }, variants)
        catalog.advance_status(tid, status.READY)

        return {"id": tid, "mixable": mixable, "grid_bpms": [v[0] for v in variants],
                "bpm": a["bpm"], "camelot": a["key"]["camelot"],
                "status": status.READY, "reused": reused}

    except Exception as exc:
        # Keep whatever work succeeded — the status high-water mark is left
        # alone so the retry resumes rather than restarting.
        with database.reading() as q:
            known = q.get_track(id=tid) is not None
        if known:
            catalog.mark_failed(tid, f"{type(exc).__name__}: {exc}")
        raise


def ingest_all(database, timer=None, force=False):
    """Ingest every configured track, skipping work already completed.

    A track already at `ready` is not touched at all — no request is made for
    it. One track failing does not abort the rest: the failure is recorded in
    that track's status_error and returned in its result entry, so a transient
    network problem costs one track rather than the whole catalog.
    """
    cfg = config.load_tracks_config()
    config.ensure_dirs()
    catalog = database.catalog
    results = []

    for entry in cfg["tracks"]:
        tid = str(entry["id"])
        if not force and catalog.status_of(tid) == status.READY:
            with database.reading() as q:
                row = q.get_track(id=tid)
                grid = [v.grid_bpm for v in q.list_variants_for_track(track_id=tid)]
            results.append({
                "id": tid, "mixable": bool(row.mixable), "grid_bpms": grid,
                "bpm": row.native_bpm, "camelot": row.camelot,
                "status": status.READY, "reused": ["all"]})
            continue
        try:
            results.append(ingest_track(database, entry, cfg["mode"], timer,
                                        force=force))
        except Exception as exc:
            with database.reading() as q:
                row = q.get_track(id=tid)
            results.append({"id": tid,
                            "status": row.status if row else status.PENDING,
                            "error": f"{type(exc).__name__}: {exc}",
                            "mixable": False, "grid_bpms": [],
                            "bpm": None, "camelot": None, "reused": []})
    return results


def failed(results):
    """The entries of an ingest_all result that did not reach ready."""
    return [r for r in results if r.get("error")]
