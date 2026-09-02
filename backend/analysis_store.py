"""Analysis arrays live in the object store, not the database.

An analysis is two very different things wearing one name. A few scalars —
tempo, key, duration, the beat grid — are small, queried, and belong in a row;
the frame and prefix arrays are large, opaque to SQL, and are only ever fetched
whole for one track at a time. Keeping both in a JSONB column put roughly
640 KB on every row, half a gigabyte across 794 tracks, and made every
``SELECT *`` drag a megabyte through the connection pooler on its way to code
that mostly did not want it.

So the arrays move to ``analysis/<track_id>.npz`` — the key
``backend/storage`` has always documented for them — and the row keeps the
scalars plus enough shape to know the arrays exist. ``hydrate`` puts the two
halves back together for the one caller that genuinely needs the frames:
transition scoring.

npz rather than JSON because these are numeric arrays and nothing else: it
stores them as binary, which is both far smaller than decimal text and far
faster to parse back. Everything is stored float64 — the prefix sums are
cumulative, so narrowing the frames they were built from makes a windowed
difference disagree with a direct sum of the same values.
"""
from __future__ import annotations

import io

import numpy as np

from . import storage

#: Series carried inside `frames` and mirrored as cumulative sums in `prefix`.
SERIES = ("rms", "flux", "bass_ratio")
#: Chroma is (n, 12) rather than 1-D, so it is packed separately.
MATRIX = ("chroma",)
#: Marks a row whose arrays were moved out. Absent on an analysis that predates
#: the move, which is how `hydrate` knows to leave it alone.
OFFLOADED = "arrays_offloaded"


def is_offloaded(analysis):
    return bool(analysis) and bool(analysis.get(OFFLOADED))


def split(analysis):
    """`(row_analysis, npz_bytes)` — the scalars, and the arrays to upload.

    Scalars that describe the arrays (`hop_dur`, `chroma_block`) stay on the
    row: they are needed to interpret a window before anything is fetched, and
    they are two floats.
    """
    frames = dict(analysis.get("frames") or {})
    prefix = dict(analysis.get("prefix") or {})
    buf = io.BytesIO()
    arrays = {}
    for name in SERIES:
        if name in frames:
            arrays[f"frames_{name}"] = np.asarray(frames.pop(name), dtype=np.float64)
        if name in prefix:
            arrays[f"prefix_{name}"] = np.asarray(prefix.pop(name), dtype=np.float64)
    for name in MATRIX:
        if name in frames:
            arrays[f"frames_{name}"] = np.asarray(frames.pop(name), dtype=np.float64)
        if name in prefix:
            arrays[f"prefix_{name}"] = np.asarray(prefix.pop(name), dtype=np.float64)
    np.savez_compressed(buf, **arrays)

    row = dict(analysis)
    row["frames"] = frames          # keeps hop_dur, chroma_block
    row["prefix"] = prefix          # normally empty
    row[OFFLOADED] = True
    return row, buf.getvalue()


def merge(row_analysis, npz_bytes):
    """Rebuild the full analysis from a row and its uploaded arrays."""
    out = dict(row_analysis)
    frames = dict(out.get("frames") or {})
    prefix = dict(out.get("prefix") or {})
    with np.load(io.BytesIO(npz_bytes)) as data:
        for key in data.files:
            target, _, name = key.partition("_")
            value = data[key].tolist()
            (frames if target == "frames" else prefix)[name] = value
    out["frames"] = frames
    out["prefix"] = prefix
    out.pop(OFFLOADED, None)
    return out


def put(track_id, analysis, store=None):
    """Upload a track's arrays; return the analysis to store on the row."""
    store = store or storage.get_store()
    row, blob = split(analysis)
    store.put_bytes(storage.analysis_key(track_id), blob,
                    content_type="application/octet-stream")
    return row


def hydrate(track_id, analysis, store=None):
    """The full analysis, fetching the arrays if the row does not carry them.

    A row written before the move still holds its own arrays and is returned
    untouched, so both layouts are readable while a migration is part-done.
    """
    if not analysis or not is_offloaded(analysis):
        return analysis
    store = store or storage.get_store()
    blob = store.get_bytes(storage.analysis_key(track_id))
    if blob is None:
        return analysis                 # arrays gone: caller sees empty frames
    return merge(analysis, blob)
