"""Reconcile the catalog against the blob store.

Two halves of the deployment hold the same facts and can drift apart: the
catalog says a track's audio is at `audio/<id>.wav` and its 128 BPM variant
runs 234.0 s, and the store either has an object at that key of that length or
it does not. Nothing kept them honest — `publish.already_done()` checks that a
key *exists*, which catches a row pointing at nothing but not a row pointing at
the wrong thing.

That gap is not hypothetical. A variant re-rendered after a fresh BPM analysis
gets a different grid and a different length; if the catalog is republished and
the objects are not (or the reverse), every key still resolves and every
duration is quietly wrong. The symptom is a mix whose transitions land in the
wrong place, which looks like a bug in the beat matcher.

    python -m backend.reconcile                    # report, touch nothing
    python -m backend.reconcile --apply            # re-upload what disagrees
    python -m backend.reconcile --apply --delete-orphans

## What is compared, and against what

The **catalog is authoritative** and the store is made to match it, not the
other way round. A row's `duration_s` is derived from the rendered audio at the
time it was written and is checked here against the master
(`variant_duration * ratio == master_duration`), so the rows are internally
consistent in a way a lone object is not; and the local render that produced
the row is still on disk, which makes "upload the file the row describes" a
repair with a source of truth behind it. Rewriting rows to match whatever the
store happens to hold would instead bake a stale render into the metadata.

Every upload is therefore guarded: the local file must itself match the row it
is being uploaded for. Where it does not, the entry is reported as unfixable
rather than pushed — the disagreement is then between the catalog and the local
disk, which this command cannot arbitrate and re-ingestion must.

## Durations without downloading

A blob's length is read from its size. Everything here is mono 16-bit PCM
written by `audio_io.save_wav` through the stdlib `wave` module, which emits a
canonical 44-byte header, so `frames = (size - 44) / 2` exactly. That makes a
whole-catalog audit a single `list` call rather than one ranged GET per object,
and the assumption is not blind: `--verify-headers` re-checks it by reading the
first 44 bytes of each object and comparing what the header declares.
"""
from __future__ import annotations

import argparse
import sys

from . import config, storage
from .db import Database
from .db.sync import redact_url

#: Bytes of canonical RIFF/WAVE header before the sample data.
WAV_HEADER_BYTES = 44
#: Bytes per frame: mono, 16-bit (see audio_io.save_wav).
WAV_BYTES_PER_FRAME = 2

#: Two durations count as equal within this. A re-upload of the very file the
#: row was written from lands at zero difference, so anything above a rounding
#: wobble is real drift, not tolerance-hunting.
TOLERANCE_S = 0.01


def duration_from_size(size, sample_rate):
    """Seconds of audio in a mono 16-bit PCM WAV of `size` bytes."""
    frames = (size - WAV_HEADER_BYTES) / WAV_BYTES_PER_FRAME
    return frames / float(sample_rate)


def _drift(source, key, stored_size, claimed, sample_rate):
    """Describe how the stored object differs from what it should be, or None.

    Two comparisons, because the delivery encoding changed what a size means:

    * **A local artifact exists** — compare byte counts. The local file IS the
      object that should be up there, so this is exact for any codec, and it
      catches a re-render at the same key regardless of format.
    * **No local artifact, PCM key** — fall back to the duration the size
      implies. Only sound for uncompressed mono 16-bit; a compressed size says
      nothing about length, and pretending otherwise would mark every encoded
      object stale forever and re-upload the catalog on every run.

    Anything else cannot be judged, so it is left alone rather than guessed at.
    """
    mine = local_size(source, key)
    if mine is not None:
        return None if mine == stored_size else "%d B stored, %d B local" % (
            stored_size, mine)
    if key.endswith(".wav"):
        actual = duration_from_size(stored_size, sample_rate)
        if abs(actual - claimed) > TOLERANCE_S:
            return "%.3fs stored, %.3fs claimed" % (actual, claimed)
    return None


def catalog_index(database):
    """`{key: (duration_s, description)}` for every object the catalog names."""
    with database.reading() as q:
        tracks = q.list_tracks()
        variants = q.list_all_variants()
    index = {}
    for t in tracks:
        if t.audio_key and t.duration_s is not None:
            index[t.audio_key] = (t.duration_s, "master %s" % t.id)
    for v in variants:
        index[v.object_key] = (v.duration_s,
                               "variant %s @ %d BPM" % (v.track_id, v.grid_bpm))
    return index


def plan(database, store, source, sample_rate=None, prefix=""):
    """Compare catalog rows with store objects.

    `source` is the local store holding the renders the rows were written
    from — the repair material. Returns a dict of lists, each entry a dict so
    the report and the apply step read the same records.

    `prefix` narrows the audit to one class of object — `audio/` for masters,
    `variants/` for rendered variants. It is applied to BOTH sides, which is
    the only safe way to do it: filtering the catalog alone would leave every
    variant in the store matching no row, and they would be reported as
    orphans and deleted by a `--delete-orphans` run that was only ever meant
    to touch masters.
    """
    sample_rate = sample_rate or config.SAMPLE_RATE
    wanted = {k: v for k, v in catalog_index(database).items()
              if k.startswith(prefix)}
    present = {k: v for k, v in store.list_blobs(prefix).items()
               if k.startswith(prefix)}

    matched, missing, stale, unfixable = [], [], [], []
    for key, (claimed, what) in sorted(wanted.items()):
        local_ok = _local_matches(source, key, claimed, sample_rate)
        if key not in present:
            entry = {"key": key, "what": what, "claimed": claimed,
                     "actual": None, "local_ok": local_ok}
            (missing if local_ok else unfixable).append(entry)
            continue
        drift = _drift(source, key, present[key], claimed, sample_rate)
        if drift is not None:
            entry = {"key": key, "what": what, "claimed": claimed,
                     "actual": drift, "local_ok": local_ok}
            (stale if local_ok else unfixable).append(entry)
        else:
            matched.append(key)

    orphans = [k for k in sorted(present) if k not in wanted]
    return {"matched": matched, "missing": missing, "stale": stale,
            "unfixable": unfixable, "orphans": orphans,
            "catalog_keys": len(wanted), "store_keys": len(present)}


def local_size(source, key):
    """Byte size of the local artifact for `key`, or None if it is not there."""
    path = source.local_path(key)
    if path is None or not path.is_file():
        return None
    return path.stat().st_size


def _local_matches(source, key, claimed, sample_rate):
    """True when the local artifact for `key` is usable as the repair source.

    For PCM the local file is checked against the duration the row claims, so a
    render that disagrees with its own row is never published. An encoded file
    carries no duration in its size, so the check reduces to "it exists" — the
    encode came from that row's PCM through `storage.put_delivery`, and the row
    was written by the same pass.
    """
    path = source.local_path(key)
    if path is None or not path.is_file():
        return False
    if not key.endswith(".wav"):
        return True
    try:
        from .audio_io import wav_duration
        return abs(wav_duration(path) - claimed) <= TOLERANCE_S
    except Exception:
        return False


def report(p, out=print):
    out("catalog names %d objects; store holds %d"
        % (p["catalog_keys"], p["store_keys"]))
    out("  agreeing            : %d" % len(p["matched"]))
    out("  absent from store   : %d" % len(p["missing"]))
    out("  present but stale   : %d" % len(p["stale"]))
    out("  orphaned in store   : %d" % len(p["orphans"]))
    if p["unfixable"]:
        out("  UNFIXABLE HERE      : %d" % len(p["unfixable"]))
    for entry in p["missing"]:
        out("    absent  %-32s %s (%.3fs)"
            % (entry["key"], entry["what"], entry["claimed"]))
    for entry in p["stale"]:
        out("    stale   %-32s %s" % (entry["key"], entry["actual"]))
    for key in p["orphans"]:
        out("    orphan  %s" % key)
    for entry in p["unfixable"]:
        out("    NO LOCAL SOURCE  %-28s %s — re-ingest this track"
            % (entry["key"], entry["what"]))


def _progress(message):
    """Progress that survives redirection.

    An apply run is minutes of uploads with nothing else to look at, and a
    buffered stdout shows none of it until the process exits — which is exactly
    when the log stops being useful.
    """
    print(message, flush=True)


#: Concurrent uploads. Each one is a `vercel blob put` subprocess that spends
#: almost all its life waiting on the network, so this is throughput, not CPU.
#: It matters at catalog scale: a 1200-track import is ~5700 objects, and one
#: at a time — process spawn included — is most of a day.
DEFAULT_WORKERS = 8


def apply_plan(p, store, source, delete_orphans=False, progress=_progress,
               workers=DEFAULT_WORKERS):
    """Upload what disagrees, and optionally remove what nothing references.

    A failed upload is recorded and the run continues. One object refused
    mid-way through thousands should not abandon the rest, and nothing is lost
    by carrying on: the re-check at the end lists whatever did not land, and
    the command is idempotent, so running it again retries exactly those.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    todo = p["missing"] + p["stale"]
    uploaded, deleted, failed = [], [], []
    lock = threading.Lock()
    done = [0]

    def upload(entry):
        key = entry["key"]
        try:
            store.put_file(key, source.local_path(key))
            outcome, record = "uploaded", uploaded
        except Exception as exc:                       # noqa: BLE001
            outcome, record = "FAILED (%s)" % exc, failed
        with lock:
            done[0] += 1
            record.append(key)
            progress("  [%d/%d] %s %s" % (done[0], len(todo), outcome, key))

    if todo:
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(upload, todo))
        else:
            for entry in todo:
                upload(entry)

    if delete_orphans:
        for key in p["orphans"]:
            progress("  deleting orphan %s" % key)
            store.delete(key)
            deleted.append(key)
    return {"uploaded": uploaded, "deleted": deleted, "failed": failed}


def verify_headers(store, keys, sample_rate=None):
    """Re-derive each object's duration from its actual WAV header.

    Guards the size arithmetic the rest of this module relies on: if an object
    were ever written with a non-canonical header, its size would imply a
    length its header contradicts, and every comparison above would be off by
    the same silent amount.
    """
    import struct
    import urllib.request
    sample_rate = sample_rate or config.SAMPLE_RATE
    disagreements = []
    for key in keys:
        url = store.url_for(key)
        req = urllib.request.Request(url, headers={"Range": "bytes=0-43"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            head = resp.read(WAV_HEADER_BYTES)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            disagreements.append((key, "not a RIFF/WAVE file"))
            continue
        channels, sr, _, _, bits = struct.unpack("<HIIHH", head[22:36])
        declared = struct.unpack("<I", head[40:44])[0]
        if (channels, sr, bits) != (1, sample_rate, 16):
            disagreements.append(
                (key, "unexpected format: %dch %dHz %dbit" % (channels, sr, bits)))
        elif declared != _size_of(store, key) - WAV_HEADER_BYTES:
            disagreements.append((key, "header declares %d data bytes" % declared))
    return disagreements


def _size_of(store, key):
    return store.list_blobs(prefix=key)[key]


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m backend.reconcile",
        description="Compare the catalog's object keys and durations against "
                    "the blob store, and make the store match.")
    ap.add_argument("--apply", action="store_true",
                    help="upload the objects that are absent or stale")
    ap.add_argument("--delete-orphans", action="store_true",
                    help="also delete store objects no row references "
                         "(destructive; requires --apply)")
    ap.add_argument("--verify-headers", action="store_true",
                    help="re-read every object's WAV header instead of "
                         "trusting its size")
    ap.add_argument("--prefix", default="", metavar="PREFIX",
                    help="only audit objects under this key prefix, e.g. "
                         "'audio/' for masters or 'variants/' for renders. "
                         "Applies to the catalog and the store alike.")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="concurrent uploads (default: %(default)s). 1 runs "
                         "them one at a time.")
    ap.add_argument("--cli", default=None, metavar="CMD",
                    help="command running the Vercel CLI, e.g. "
                         "'npx --yes vercel@latest' when it is not installed")
    args = ap.parse_args(argv)

    if args.delete_orphans and not args.apply:
        print("error: --delete-orphans needs --apply", file=sys.stderr)
        return 2

    database = Database.from_config()
    store = storage.get_store()
    if args.cli and isinstance(store, storage.VercelBlobStore):
        store.cli = args.cli.split()
    source = storage.LocalBlobStore(root=config.DATA_DIR)

    print("catalog : %s" % redact_url(config.database_url()))
    print("store   : %s" % type(store).__name__)
    if args.prefix:
        print("prefix  : %s  (everything outside it is left alone)"
              % args.prefix)
    p = plan(database, store, source, prefix=args.prefix)
    report(p)

    if args.verify_headers:
        checked = p["matched"]
        print("re-reading %d WAV headers ..." % len(checked))
        bad = verify_headers(store, checked)
        print("  header disagreements: %d" % len(bad))
        for key, why in bad:
            print("    %s: %s" % (key, why))

    if args.apply:
        if not (p["missing"] or p["stale"] or (args.delete_orphans and p["orphans"])):
            print("nothing to do")
        else:
            print("applying ...")
            done = apply_plan(p, store, source,
                              delete_orphans=args.delete_orphans,
                              workers=args.workers)
            print("uploaded %d, deleted %d, failed %d"
                  % (len(done["uploaded"]), len(done["deleted"]),
                     len(done["failed"])))
            for key in done["failed"]:
                print("  failed: %s" % key)
            print("re-checking ...")
            report(plan(database, store, source, prefix=args.prefix))
    database.dispose()
    return 1 if p["unfixable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
