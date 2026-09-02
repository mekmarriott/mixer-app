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
from . import (bpm_grid, config, db, jamendo, licensing, segmentation, storage,
               stretch)
from .audio_io import load_wav, save_wav, wav_duration
from .timing import Timer

status = db.status


# Audio is addressed by object key rather than filesystem path
# (backend/storage.py), so the same pipeline works against local disk in
# development and a cloud bucket in deployment. The local backend still hands
# back a real path, which is what keeps the resume checks below — `_usable`,
# and re-rendering only the variants that are actually missing — working
# unchanged against the same files on disk.
def _master_path(track_id, store=None):
    """The PCM master, which is what resuming reads back.

    `master_source_key`, not `master_key`: the latter names the compressed copy
    the browser downloads, and everything here — reuse-after-crash, re-render
    of missing variants — needs samples to compute from, not bytes to serve.
    """
    store = store or storage.get_store()
    return store.local_path(storage.master_source_key(track_id))


def _variant_path(track_id, grid_bpm, store=None):
    store = store or storage.get_store()
    return store.local_path(storage.variant_key(track_id, grid_bpm))


def _usable(path):
    if path is None:
        return False              # remote store: nothing local to resume from
    path = Path(path)
    return path.exists() and path.stat().st_size > 44      # bigger than a WAV header


def _reject_unmixable_license(meta):
    """Refuse a track whose licence forbids the only thing we would do with it.

    Called after the metadata request and before the audio download. ND
    licences prohibit derivative works, and every BPM-grid variant is one
    (requirements.md §2), so an ND track can never be mixed — it would be
    downloaded, analysed, and then permanently excluded from the feature the
    catalogue exists for.

    Rejecting here is what makes that free. The licence is already known from
    the metadata; the download is the expensive part and has not happened yet.
    On the live catalogue 64 of 72 tracks are ND, so this is most of the work
    and most of the metered API quota.
    """
    lic = licensing.parse_license(meta["license"])   # unknown licences raise
    if lic["nd"]:
        exc = jamendo.IncompatibleLicense(
            f"{meta['id']}: {meta['license']} forbids derivative works, so no "
            f"tempo-matched variant may be rendered — skipped before download")
        exc.meta = meta
        raise exc


def _record_unmixable(catalog, tid, entry, meta):
    """Store the licence decision without storing the track's audio.

    The row is deliberately kept: requirements.md §1 wants the specific CC
    variant recorded per track, and having it here means the decision is not
    re-litigated — and the metadata request is not repeated — on every restart.

    What it does NOT have is audio, analysis, segments or variants. It is
    marked unmixable, so backend/deck.py leaves it out of the browse deck and
    matching never offers it.
    """
    lic = licensing.parse_license(meta["license"])
    catalog.save_ingested_track({
        **meta, **lic,
        "genre": meta.get("genre") or entry.get("genre", ""),
        "mixable": False,
        "audio_key": None,
        "status": status.READY,        # nothing further will ever be done
    })
    catalog.advance_status(tid, status.READY)
    return {"id": tid, "mixable": False, "grid_bpms": [], "bpm": None,
            "camelot": None, "status": status.READY, "reused": [],
            "skipped": "license", "license": meta["license"]}


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

    # Already known to be unmixable: nothing about a licence changes between
    # runs, so re-requesting the metadata just to reach the same conclusion is
    # pure waste. `force` still re-checks, in case the catalogue was wrong.
    if not force and existing is not None and not existing.mixable:
        return {"id": tid, "mixable": False, "grid_bpms": [], "bpm": None,
                "camelot": None, "status": existing.status, "reused": ["license"],
                "skipped": "license", "license": existing.license}

    try:
        # ---------------------------------------------------- fetch (network)
        have_master = (not force and existing is not None
                       and status.at_least(existing.status, status.FETCHED)
                       and _usable(_master_path(tid)))
        if have_master:
            with timer.stage("reuse_master", tid):
                samples, sr = load_wav(_master_path(tid))
            meta = {"id": tid, "name": existing.name, "artist": existing.artist,
                    "genre": existing.genre, "license": existing.license,
                    "audiodownload_allowed": True,
                    "source_url": existing.source_url or ""}
            lic = licensing.parse_license(meta["license"])
            reused.append("fetch")
        else:
            try:
                with timer.stage("fetch", tid):
                    meta, samples, sr = jamendo.fetch_track(
                        entry, mode, accept=_reject_unmixable_license)
            except jamendo.IncompatibleLicense as skip:
                return _record_unmixable(catalog, tid, entry, skip.meta)
            # Unknown licenses raise here, before anything is persisted (P1-07).
            lic = licensing.parse_license(meta["license"])
            with timer.stage("persist_master", tid):
                # Two files: the PCM master every variant is rendered from, and
                # the compressed copy the browser downloads. audio_key names the
                # second — the first is never served.
                jamendo.persist_master(meta, samples, sr)
                audio_key = storage.put_delivery(
                    storage.get_store(), storage.master_key(tid), samples, sr)
            catalog.save_ingested_track({
                **meta, **lic,
                "mixable": not lic["nd"],
                "audio_key": audio_key,
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
                "audio_key": storage.master_key(tid),
                "analysis": a,
                "segments": segs,
                "status": status.ANALYZED,
            })
            catalog.advance_status(tid, status.ANALYZED)

        # --------------------------------------------------------- variants
        grid, variants, rendered = [], [], 0
        if mixable:
            meta["genre"] = bpm_grid.resolve_bucket(meta.get("genre"), a["bpm"]) or ""
            grid = bpm_grid.grid_points(a["bpm"], meta["genre"] or None)
            to_render = []
            for g in grid:
                vpath = _variant_path(tid, g)
                if not force and _usable(vpath):
                    # Already on disk from an interrupted run: re-register it
                    # rather than paying for the stretch again.
                    variants.append((g, g / a["bpm"], storage.variant_key(tid, g),
                                     wav_duration(vpath)))
                else:
                    to_render.append(g)
            if to_render:
                with timer.stage("render_variants", tid):
                    for g in to_render:
                        ratio = g / a["bpm"]
                        out = stretch.stretch(samples, sr, ratio)
                        vpath = _variant_path(tid, g)
                        save_wav(vpath, out, sr)
                        variants.append((g, ratio, storage.variant_key(tid, g),
                                         len(out) / sr))
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
            "audio_key": storage.master_key(tid),
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
