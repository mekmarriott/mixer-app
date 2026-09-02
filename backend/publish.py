"""Parallel batch ingestion — the local half of the deployment split.

Ingestion does not run on Vercel: no ffmpeg, no rubberband, a read-only
filesystem and a function timeout an order of magnitude shorter than one
track's render (docs/infrastructure-plan.md §1.2, §5.2). It runs here instead,
on a machine that already has the binaries, and publishes its output to the
blob store and the catalog database.

## Why this is not just `ingest_all` in a loop

Measured cost is ~60 s/track for a 4-minute track through Rubber Band at the
current grid, so a 10,000-track catalog is ~167 core-hours. Sequentially that
is a week. The work is embarrassingly parallel per track, so the wall-clock
cost is really (167 / cores) hours — under a day on a laptop, two overnight
runs.

## Two pools, deliberately

Downloads and rendering are separated because they have opposite constraints:

* **Network stage — threads, globally rate limited.** Jamendo publishes no
  rate limit and exposes no rate-limit headers, so the limiter is the only
  thing standing between a 10k-track run and an unintentional flood. It is
  shared process-wide: raising CPU parallelism must never raise request rate.
* **CPU stage — processes.** Analysis and time-stretch are CPU-bound and
  release no GIL worth having, so threads would not help.

Only object *keys* cross the process boundary, never audio arrays: a 4-minute
master is ~42 MB as float64 and pickling that per task would cost more than
the render it feeds.

## Resumability

Every run skips tracks already present in the catalog with their variants
rendered and their blobs in place. A 10k-track import will be interrupted —
by a laptop sleeping, a network blip, or an exhausted API budget — and the
correct response to any of those is to run the same command again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

from . import bpm_grid, config, jamendo, licensing, ratelimit, storage
from . import analysis as analysis_mod
from . import segmentation, stretch
from .audio_io import load_wav, save_wav


def _write_variant(store, tid, grid_bpm, samples, sr):
    """Persist one rendered variant through the blob store, returning its key.

    The local backend hands back a real path so the render goes straight to its
    final location; a remote backend stages to a temp file and uploads.
    """
    key = storage.variant_key(tid, grid_bpm)
    dst = store.local_path(key)
    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        save_wav(dst, samples, sr)
        return key
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        save_wav(tmp, samples, sr)
        return store.put_file(key, tmp, "audio/wav")
    finally:
        os.unlink(tmp)



# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def already_done(database, store, entry_id):
    """True when this track is fully published and can be skipped.

    Checks the blobs as well as the rows: a catalog row whose audio never made
    it to the store is worse than a missing row, because the API will happily
    hand clients a 302 to a URL that 404s.
    """
    with database.reading() as q:
        t = q.get_track(id=str(entry_id))
        if not t or not t.audio_key:
            return False
        variants = q.list_variants_for_track(track_id=str(entry_id))
    if not store.exists(t.audio_key):
        return False
    if not t.mixable:
        return True                      # ND: master only, by design
    if not variants:
        return False
    return all(store.exists(v.object_key) for v in variants)


def plan(database, store, entries):
    """Split the catalog into (todo, skipped)."""
    todo, skipped = [], []
    for e in entries:
        (skipped if already_done(database, store, e["id"]) else todo).append(e)
    return todo, skipped


def budget_report(todo, mode):
    """What this run will cost against the API quota, before spending any.

    The free tier is commonly quoted at 35,000 requests/month, but the API
    returns no quota headers and no usage field, so there is no way to read
    actual remaining quota from the service. Printing the projection before
    starting is the only check available.
    """
    if mode != "jamendo":
        return {"metadata_requests": 0, "downloads": 0}
    meta_reqs = jamendo.estimate_api_requests(len(todo))
    return {"metadata_requests": meta_reqs, "downloads": len(todo)}


# --------------------------------------------------------------------------
# Stage 1 — fetch masters (network, rate limited)
# --------------------------------------------------------------------------

def _load_cached_meta(store, track_id):
    """Source metadata recorded when the master was ingested, or None."""
    key = storage.meta_key(track_id)
    if not store.exists(key):
        return None
    path = store.local_path(key)
    if path is None:
        return None                      # remote store: not worth a round trip
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None                      # corrupt sidecar: re-fetch rather than guess


def fetch_masters(entries, mode, store, io_workers=2, api_limiter=None,
                  dl_limiter=None, budget=None, refetch=False, log=print):
    """Resolve metadata and put every master in the store. Returns [(meta, key)].

    Offline mode synthesises instead of downloading, so it skips the network
    entirely and stays single-threaded — there is nothing to overlap.
    """
    if mode == "offline":
        out = []
        for e in entries:
            meta, samples, sr = jamendo.fetch_track(e, mode)
            key = jamendo.persist_master(meta, samples, sr, store=store)
            out.append((meta, key))
        return out

    api_limiter = api_limiter or ratelimit.TokenBucket(jamendo.DEFAULT_API_RATE)
    dl_limiter = dl_limiter or ratelimit.TokenBucket(jamendo.DEFAULT_DOWNLOAD_RATE)

    # Anything whose master AND metadata sidecar are already in the store costs
    # nothing to reuse. This is what makes rebuilding the catalog free: wiping
    # the database (or re-rendering variants after a grid change) would
    # otherwise re-download every track and re-spend monthly API quota to
    # re-learn metadata already on disk. Only `--refetch` overrides it.
    cached, need_fetch = [], []
    for e in entries:
        tid = str(e["id"])
        meta = None if refetch else _load_cached_meta(store, tid)
        if meta is not None and store.exists(storage.master_key(tid)):
            cached.append((meta, storage.master_key(tid)))
        else:
            need_fetch.append(e)
    if cached:
        log(f"[reuse] {len(cached)} master(s) already in the store — "
            f"no API requests, no downloads")
    if not need_fetch:
        return cached

    genre_by_id = {str(e["id"]): e.get("genre", "house") for e in need_fetch}
    log(f"[meta] {len(need_fetch)} track(s) in "
        f"{jamendo.estimate_api_requests(len(need_fetch))} batched request(s)")
    metas = jamendo.fetch_metadata([e["id"] for e in need_fetch],
                                   genre_by_id=genre_by_id,
                                   limiter=api_limiter, budget=budget)

    def one(meta):
        encoded = jamendo.download_audio(meta["audiodownload"],
                                         limiter=dl_limiter)
        samples, sr = jamendo.decode_to_samples(encoded)
        return meta, jamendo.persist_master(meta, samples, sr, store=store)

    out = list(cached)
    # Bounded concurrency; the limiter caps the rate regardless, but an
    # unbounded pool would still open every socket at once.
    with ThreadPoolExecutor(max_workers=io_workers) as pool:
        futures = {pool.submit(one, m): m["id"] for m in metas.values()}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                out.append(fut.result())
            except Exception as exc:                     # noqa: BLE001
                log(f"[fetch] {tid}: FAILED {type(exc).__name__}: {exc}")
    return out


# --------------------------------------------------------------------------
# Stage 2 — analyse and render (CPU, parallel across processes)
# --------------------------------------------------------------------------

_WORKER_STORE = None


def _worker_init(data_dir, backend):
    """Rebuild module state in a spawned worker.

    ProcessPoolExecutor uses spawn on macOS, so workers re-import this package
    from scratch and do not inherit the parent's in-process configuration.
    """
    global _WORKER_STORE
    os.environ["DJMIXER_DATA"] = str(data_dir)
    config.DATA_DIR = Path(data_dir)
    config.AUDIO_DIR = config.DATA_DIR / "audio"
    config.VARIANT_DIR = config.DATA_DIR / "variants"
    config.ensure_dirs()
    storage.reset_store()
    _WORKER_STORE = storage.make_store(backend)


def _render(meta, master_key, store=None):
    """Analyse one master and render its variants. Returns a row dict.

    Runs in a worker process. Reads the master back from the store by key
    rather than receiving samples, so the only things pickled are strings.
    """
    store = store or _WORKER_STORE or storage.get_store()
    path = store.local_path(master_key)
    if path is None:
        raise RuntimeError(
            "the CPU stage needs a locally readable master; run the publisher "
            "against the local store and upload afterwards")
    samples, sr = load_wav(path)

    a = analysis_mod.analyze(samples, sr)
    segs = segmentation.segment(a)
    lic = licensing.parse_license(meta["license"])
    mixable = not lic["nd"]

    variants = []
    if mixable:
        for g in bpm_grid.grid_points(a["bpm"], meta["genre"]):
            ratio = g / a["bpm"]
            out = stretch.stretch(samples, sr, ratio)
            vkey = _write_variant(store, meta["id"], g, out, sr)
            variants.append((g, ratio, vkey, len(out) / sr))

    return {
        **meta, **lic,
        "mixable": mixable,
        "native_bpm": a["bpm"],
        "camelot": a["key"]["camelot"],
        "duration_s": a["duration_s"],
        "audio_key": master_key,
        "analysis": a,
        "segments": segs,
        "variants": variants,
    }


def _render_task(args):
    return _render(*args)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def publish(database, entries, mode, store=None, workers=None, io_workers=2,
            api_rate=None, download_rate=None, request_budget=None,
            refetch=False, log=print):
    """Ingest `entries` in parallel and write them to `database` and the store.

    Database writes all happen here, in the calling thread. Worker processes
    return plain dicts and never touch the connection, so the write path is
    serialised for free and no connection is shared across processes — which
    would be unsound regardless of how the request path handles concurrency.
    """
    store = store or storage.get_store()
    workers = workers or (os.cpu_count() or 4)

    todo, skipped = plan(database, store, entries)
    if skipped:
        log(f"[plan] skipping {len(skipped)} already-published track(s)")
    if not todo:
        log("[plan] nothing to do")
        return []

    cost = budget_report(todo, mode)
    log(f"[plan] {len(todo)} track(s); ~{cost['metadata_requests']} metadata "
        f"request(s) + {cost['downloads']} download(s)")

    budget = ratelimit.RequestBudget(request_budget) if request_budget else None
    api_limiter = ratelimit.TokenBucket(api_rate or jamendo.DEFAULT_API_RATE)
    dl_limiter = ratelimit.TokenBucket(download_rate or jamendo.DEFAULT_DOWNLOAD_RATE)

    t0 = time.monotonic()
    fetched = fetch_masters(todo, mode, store, io_workers=io_workers,
                            api_limiter=api_limiter, dl_limiter=dl_limiter,
                            budget=budget, refetch=refetch, log=log)
    log(f"[fetch] {len(fetched)}/{len(todo)} master(s) in "
        f"{time.monotonic() - t0:.1f}s")

    results = []
    tasks = [(meta, key) for meta, key in fetched]

    if workers == 1:
        # Inline path: no pool. Keeps the publisher testable without spawning
        # processes, and is the right choice for tiny catalogs.
        rows = []
        for args in tasks:
            try:
                rows.append(_render(*args, store=store))
            except Exception as exc:                     # noqa: BLE001
                log(f"[render] {args[0]['id']}: FAILED {type(exc).__name__}: {exc}")
    else:
        rows = []
        backend = "local" if isinstance(store, storage.LocalBlobStore) else "vercel"
        with ProcessPoolExecutor(
                max_workers=workers, initializer=_worker_init,
                initargs=(str(config.DATA_DIR), backend)) as pool:
            futures = {pool.submit(_render_task, args): args[0]["id"]
                       for args in tasks}
            for fut in as_completed(futures):
                tid = futures[fut]
                try:
                    rows.append(fut.result())
                except Exception as exc:                 # noqa: BLE001
                    log(f"[render] {tid}: FAILED {type(exc).__name__}: {exc}")

    for row in rows:
        variants = row.pop("variants", [])
        # One transaction per track, in this process only: workers return
        # plain dicts and never touch the database.
        database.catalog.save_ingested_track(row, variants)
        results.append({"id": row["id"], "mixable": row["mixable"],
                        "grid_bpms": [v[0] for v in variants],
                        "bpm": row["native_bpm"], "camelot": row["camelot"]})
        log(f"  published {row['id']} bpm={row['native_bpm']} "
            f"key={row['camelot']} variants={len(variants)}")

    log(f"[done] {len(results)} track(s) in {time.monotonic() - t0:.1f}s")
    return results


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m backend.publish",
        description="Batch-ingest a catalog locally and publish it to the "
                    "blob store and database.")
    p.add_argument("--workers", type=int, default=os.cpu_count(),
                   help="CPU worker processes for analysis/rendering "
                        "(default: all cores). 1 runs inline.")
    p.add_argument("--io-workers", type=int, default=2,
                   help="concurrent downloads (default: 2). Only 1 is "
                        "verified safe against Jamendo; raise carefully.")
    p.add_argument("--api-rate", type=float, default=jamendo.DEFAULT_API_RATE,
                   help="max metadata requests/sec (default: %(default)s)")
    p.add_argument("--download-rate", type=float,
                   default=jamendo.DEFAULT_DOWNLOAD_RATE,
                   help="max audio downloads/sec (default: %(default)s)")
    p.add_argument("--request-budget", type=int, default=None,
                   help="hard ceiling on API requests for this run; the run "
                        "aborts rather than exceeding it")
    p.add_argument("--limit", type=int, default=None,
                   help="only publish the first N pending tracks")
    p.add_argument("--refetch", action="store_true",
                   help="re-download masters even when they are already in "
                        "the store. Spends API quota — the default reuses "
                        "local masters so a catalog rebuild is free.")
    p.add_argument("--dry-run", action="store_true",
                   help="report the plan and API cost, then exit")
    args = p.parse_args(argv)

    cfg = config.load_tracks_config()
    config.ensure_dirs()
    store = storage.get_store()
    from .db import Database
    database = Database.from_config().migrate()

    entries = cfg["tracks"]
    todo, skipped = plan(database, store, entries)
    if args.limit:
        todo = todo[:args.limit]

    if args.dry_run:
        cost = budget_report(todo, cfg["mode"])
        print(f"mode={cfg['mode']} pending={len(todo)} skipped={len(skipped)}")
        print(f"metadata requests: ~{cost['metadata_requests']}")
        print(f"audio downloads:   {cost['downloads']}")
        print("(downloads hit a separate storage host and are assumed not to "
              "count against the API quota; this is unverified)")
        return 0

    publish(database, todo, cfg["mode"], store=store, workers=args.workers,
            io_workers=args.io_workers, api_rate=args.api_rate,
            download_rate=args.download_rate,
            request_budget=args.request_budget, refetch=args.refetch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
