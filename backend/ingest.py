"""Ingestion pipeline (Phase 1). Orchestrates:

  fetch -> license gate -> persist master -> analyze (once, native tempo)
  -> segment -> plan BPM grid -> render variants (ND excluded) -> cache

Each stage is latency-instrumented (docs/latency-report.md is generated from
these measurements by backend/benchmark.py).

Audio rendering happens outside the database transaction: variants are written
to disk first and committed with the track in a single write at the end, so a
crash mid-render cannot leave a track in the catalog advertising variants that
were never recorded.
"""
from . import analysis as analysis_mod
from . import bpm_grid, config, jamendo, licensing, segmentation, stretch
from .audio_io import save_wav
from .timing import Timer


def ingest_track(database, entry, mode, timer=None):
    timer = timer or Timer(database)
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

    variants = []
    if mixable:
        with timer.stage("render_variants", tid):
            for g in bpm_grid.grid_points(a["bpm"], meta["genre"]):
                ratio = g / a["bpm"]
                out = stretch.stretch(samples, sr, ratio)
                vpath = config.VARIANT_DIR / f"{tid}_{g}.wav"
                save_wav(vpath, out, sr)
                variants.append((g, ratio, str(vpath), len(out) / sr))

    database.catalog.save_ingested_track(row, variants)
    return {"id": tid, "mixable": mixable, "grid_bpms": [v[0] for v in variants],
            "bpm": a["bpm"], "camelot": a["key"]["camelot"]}


def ingest_all(database, timer=None):
    cfg = config.load_tracks_config()
    config.ensure_dirs()
    results = []
    for entry in cfg["tracks"]:
        results.append(ingest_track(database, entry, cfg["mode"], timer))
    return results
