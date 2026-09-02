"""Waveform envelopes — computed once at startup, served from memory.

A track's envelope is derived entirely from its cached analysis, which never
changes after ingestion. There is therefore no reason to recompute it (or to
re-read the multi-megabyte `analysis_json` blob) on every page load. The
warmup pass builds every envelope the UI can ask for before the server reports
ready, and requests are answered from this cache without touching the DB.

That removes the deck's request-per-row fan-out, which was both the dominant
source of boot latency and the trigger for the API-01 read race.
"""
import threading

import numpy as np

from . import config


def envelope(analysis, points, bpm=None):
    """Downsampled, peak-normalized RMS envelope plus the timing metadata the
    client needs. `bpm` rescales times onto a grid variant; None = native.

    Extracted verbatim from the original /waveform endpoint so the cached and
    on-demand paths cannot drift.
    """
    ratio = (bpm / analysis["bpm"]) if bpm else 1.0
    rms = np.asarray(analysis["frames"]["rms"])
    idx = np.linspace(0, len(rms) - 1, points).astype(int)
    env = rms[idx]
    peak = env.max() or 1.0
    return {
        "points": (env / peak).round(4).tolist(),
        "duration_s": analysis["duration_s"] / ratio,
        "hop_dur": analysis["frames"]["hop_dur"] / ratio,
        "beat_grid": [b / ratio for b in analysis["beat_grid"]],
        "bpm": analysis["bpm"] * ratio,
    }


def rescale_envelope(native, bpm=None):
    """A stored native envelope, presented at `bpm`.

    envelope() samples `points` from the analysis frames by index, which does
    not depend on the grid the track is stretched to — only the timing scalars
    beside them do, all by the same ratio. So this reproduces
    ``envelope(analysis, points, bpm)`` exactly from a stored native result,
    without reading the analysis at all.
    """
    if not native:
        return None
    ratio = (bpm / native["bpm"]) if bpm else 1.0
    if ratio == 1.0:
        return dict(native)
    return {
        "points": native["points"],
        "duration_s": native["duration_s"] / ratio,
        "hop_dur": native["hop_dur"] / ratio,
        "beat_grid": [b / ratio for b in native["beat_grid"]],
        "bpm": native["bpm"] * ratio,
    }


class WaveformCache:
    """Thread-safe (track_id, points, bpm) -> envelope.

    Reads take no lock — dict lookups are atomic under the GIL and entries are
    immutable once written — so the hot path costs nothing.
    """

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(track_id, points, bpm=None):
        return (str(track_id), int(points), int(bpm) if bpm else None)

    def get(self, track_id, points, bpm=None):
        return self._data.get(self.key(track_id, points, bpm))

    def put(self, track_id, points, bpm, value):
        with self._lock:
            self._data[self.key(track_id, points, bpm)] = value
        return value

    def get_or_compute(self, track_id, points, bpm, analysis_loader):
        """Cache hit, or compute from a freshly loaded analysis and store it.

        Only the grid-variant envelopes needed after a pair is chosen should
        ever miss — every native envelope is warmed at startup.
        """
        hit = self.get(track_id, points, bpm)
        if hit is not None:
            return hit
        analysis = analysis_loader()
        if analysis is None:
            return None
        return self.put(track_id, points, bpm, envelope(analysis, points, bpm))

    def __len__(self):
        return len(self._data)

    def warm_native(self, track_id, analysis):
        """Precompute both envelopes the UI asks for at native tempo."""
        for pts in (config.DECK_WAVEFORM_POINTS, config.TIMELINE_WAVEFORM_POINTS):
            self.put(track_id, pts, None, envelope(analysis, pts, None))
