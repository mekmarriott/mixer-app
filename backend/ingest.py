"""Ingestion pipeline (Phase 1). Orchestrates:

  fetch -> license gate -> persist master -> analyze (once, native tempo)
  -> segment -> plan BPM grid -> render variants (ND excluded) -> cache

Each stage is latency-instrumented (docs/latency-report.md is generated from
these measurements by backend/benchmark.py)."""
from . import analysis as analysis_mod
from . import bpm_grid, config, db, jamendo, licensing, segmentation, stretch
from .audio_io import save_wav
from .timing import Timer


def ingest_track(con, entry, mode, timer=None):
    timer = timer or Timer(con)
    tid = str(entry["id"])

    with timer.stage("fetch", tid):
        meta, samples, sr = jamendo.fetch_track(entry, mode)

    lic = licensing.parse_license(meta["license"])            # raises on unknown (P1-07)

    with timer.stage("persist_master", tid):
        audio_path = jamendo.persist_master(meta, samples, sr)

    with timer.stage("analyze", tid):
        a = analysis_mod.analyze(samples, sr)

    with timer.stage("segment", tid):
        segs = segmentation.segment(a)

    mixable = not lic["nd"]                                    # ND => playback only (P1-08)
    row = {
        **meta, **lic,
        "mixable": mixable,
        "native_bpm": a["bpm"],
        "camelot": a["key"]["camelot"],
        "duration_s": a["duration_s"],
        "audio_path": str(audio_path),
        "analysis": a,
        "segments": segs,
    }
    db.upsert_track(con, row)

    rendered = []
    if mixable:
        with timer.stage("render_variants", tid):
            for g in bpm_grid.grid_points(a["bpm"], meta["genre"]):
                ratio = g / a["bpm"]
                out = stretch.stretch(samples, sr, ratio)
                vpath = config.VARIANT_DIR / f"{tid}_{g}.wav"
                save_wav(vpath, out, sr)
                db.add_variant(con, tid, g, ratio, vpath, len(out) / sr)
                rendered.append(g)
    return {"id": tid, "mixable": mixable, "grid_bpms": rendered,
            "bpm": a["bpm"], "camelot": a["key"]["camelot"]}


def ingest_all(con, timer=None):
    cfg = config.load_tracks_config()
    config.ensure_dirs()
    results = []
    for entry in cfg["tracks"]:
        results.append(ingest_track(con, entry, cfg["mode"], timer))
    return results
