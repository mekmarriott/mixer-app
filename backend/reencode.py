"""Re-encode an existing catalog into the delivery format.

Masters and variants used to be served as the same 16-bit PCM they are
rendered as. They are now served compressed (see config.AUDIO_DELIVERY_CODEC),
which changes both the bytes and the object key — `audio/1001.wav` becomes
`audio/1001.m4a`. A catalog ingested before that change has neither, so the
rows point at keys that will never resolve.

    python -m backend.reencode              # report what is missing
    python -m backend.reencode --apply      # encode, and repoint the rows

This encodes from the PCM already on disk rather than re-rendering. That
matters for variants: re-rendering one means running the time-stretch again,
which is minutes of CPU per track, whereas the stretched PCM is sitting in
data/variants and encoding it is a fraction of a second. The two produce the
same audio — the stretch happens before the encode either way.

It does NOT upload. Objects reach the store through the publisher, and an
already-published catalog through `python -m backend.reconcile --apply`, which
will see the new keys as absent and push them. Run this first, then that.

The PCM is left on disk. It is the source every variant is rendered from and
the only thing that can produce a different encoding later without going back
to Jamendo, so deleting it is a separate decision (`make clean-variants`).
"""
from __future__ import annotations

import argparse
import sys

from . import config, storage
from .audio_io import load_wav
from .db import Database
from .db.engine import DatabaseError


def _plan_one(store, src_key, dst_key):
    """`(action, reason)` for a single object.

    `encode` — the PCM is here and the delivery object is not.
    `present` — already done.
    `missing-source` — nothing local to encode from; needs re-ingesting or a
    download from the store, neither of which this command does.
    """
    if store.exists(dst_key):
        return "present", None
    src = store.local_path(src_key)
    if src is None or not src.is_file():
        return "missing-source", f"no local PCM at {src_key}"
    return "encode", None


def plan(database, store):
    """What each track and variant needs. Rows are dicts so the report and the
    apply pass read the same records."""
    with database.reading() as q:
        tracks = q.list_tracks()
        variants = q.list_all_variants()

    items = []
    for t in tracks:
        if not t.audio_key:
            continue                       # unmixable, never downloaded
        dst = storage.master_key(t.id)
        action, why = _plan_one(store, storage.master_source_key(t.id), dst)
        items.append({"kind": "master", "id": t.id, "grid_bpm": None,
                      "src": storage.master_source_key(t.id), "dst": dst,
                      "action": action, "why": why,
                      "repoint": t.audio_key != dst})
    for v in variants:
        src = storage.variant_key(v.track_id, v.grid_bpm, ext="wav")
        dst = storage.variant_key(v.track_id, v.grid_bpm)
        action, why = _plan_one(store, src, dst)
        items.append({"kind": "variant", "id": v.track_id,
                      "grid_bpm": v.grid_bpm, "src": src, "dst": dst,
                      "action": action, "why": why,
                      "repoint": v.object_key != dst})
    return items


def report(items, out=print):
    counts = {}
    for it in items:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    out("codec   : %s @ %s -> .%s"
        % (config.AUDIO_DELIVERY_CODEC, config.AUDIO_DELIVERY_BITRATE,
           config.delivery_ext()))
    out("objects : %d" % len(items))
    out("  to encode        : %d" % counts.get("encode", 0))
    out("  already encoded  : %d" % counts.get("present", 0))
    out("  no local PCM     : %d" % counts.get("missing-source", 0))
    out("rows to repoint    : %d" % sum(1 for it in items if it["repoint"]))
    for it in items:
        if it["action"] == "missing-source":
            out("    SKIP %s %s: %s" % (it["kind"], it["id"], it["why"]))


def encode_all(items, store, progress=print):
    """Encode everything the plan marked, returning the keys written."""
    todo = [it for it in items if it["action"] == "encode"]
    written = []
    for i, it in enumerate(todo, 1):
        progress("  [%d/%d] %s -> %s" % (i, len(todo), it["src"], it["dst"]))
        samples, sr = load_wav(store.local_path(it["src"]))
        storage.put_delivery(store, it["dst"], samples, sr)
        written.append(it["dst"])
    return written


def repoint(database, items, progress=print):
    """Point the rows at the delivery keys.

    Only rows whose object now exists are moved. Repointing one whose encode
    failed or was skipped would replace a key that resolves with one that does
    not, turning a stale object into a missing one.
    """
    movable = {it["dst"] for it in items if it["action"] != "missing-source"}
    moved = 0

    def write(q):
        nonlocal moved
        for t in q.list_tracks():
            dst = storage.master_key(t.id)
            if not t.audio_key or t.audio_key == dst or dst not in movable:
                continue
            q.upsert_track(
                id=t.id, name=t.name, artist=t.artist, genre=t.genre,
                license=t.license, license_nd=t.license_nd,
                license_sa=t.license_sa, license_nc=t.license_nc,
                mixable=t.mixable, native_bpm=t.native_bpm,
                camelot=t.camelot, duration_s=t.duration_s,
                audio_key=dst, analysis_json=t.analysis_json,
                segments_json=t.segments_json, status=t.status,
                status_error=t.status_error, source_url=t.source_url,
                fetched_at=t.fetched_at, analyzed_at=t.analyzed_at,
                ready_at=t.ready_at)
            moved += 1
        for v in q.list_all_variants():
            dst = storage.variant_key(v.track_id, v.grid_bpm)
            if v.object_key == dst or dst not in movable:
                continue
            q.upsert_variant(track_id=v.track_id, grid_bpm=v.grid_bpm,
                             ratio=v.ratio, object_key=dst,
                             duration_s=v.duration_s)
            moved += 1

    database.run(write, write=True)
    progress("repointed %d rows" % moved)
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m backend.reencode",
        description="Encode an existing catalog into the delivery format and "
                    "point its rows at the new object keys. Uploads nothing.")
    ap.add_argument("--apply", action="store_true",
                    help="encode and rewrite the rows (default: report only)")
    args = ap.parse_args(argv)

    if config.AUDIO_DELIVERY_CODEC == "wav":
        print("delivery codec is 'wav' — nothing to re-encode", file=sys.stderr)
        return 0

    database = Database.from_config()
    store = storage.get_store()
    try:
        items = plan(database, store)
        report(items)
        if args.apply:
            print("encoding ...")
            written = encode_all(items, store)
            print("encoded %d objects" % len(written))
            repoint(database, items)
            print("re-checking ...")
            report(plan(database, store))
        else:
            print("\n(report only — pass --apply to encode and repoint)")
    except DatabaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
