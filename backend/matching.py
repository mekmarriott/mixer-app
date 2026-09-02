"""Matching & recommendation (Phase 2).

score = W_BPM * bpm_score + W_KEY * key_score + W_ENERGY * energy_score

bpm_score    near-binary from the shared grid: 1 minus a small penalty for
             how far each track must stretch from native tempo to meet.
key_score    Camelot-wheel lookup (24x24, precomputed).
energy_score outro-of-A vs intro-of-B mean RMS closeness (O(1) via prefix sums).
"""
import numpy as np

from . import bpm_grid, config
from .analysis import window_mean
from .db.catalog import grid_bpms_by_track

_NUMS = list(range(1, 13))


def _camelot_parse(c):
    return int(c[:-1]), c[-1]           # (number 1..12, letter A/B)


def _wheel_dist(a, b):
    d = abs(a - b)
    return min(d, 12 - d)


def camelot_score(ca, cb):
    """Standard DJ harmonic compatibility on the Camelot wheel."""
    na, la = _camelot_parse(ca)
    nb, lb = _camelot_parse(cb)
    dist = _wheel_dist(na, nb)
    if ca == cb:
        return 1.0
    if la == lb:                        # same mode
        if dist == 1:
            return 0.8
        if dist == 2:
            return 0.4
        return max(0.0, 0.25 - 0.05 * (dist - 3))
    # cross-mode
    if dist == 0:                       # relative major/minor
        return 0.8
    if dist == 1:
        return 0.35
    return 0.1


CAMELOT_TABLE = {(f"{n}{l}", f"{m}{k}"): camelot_score(f"{n}{l}", f"{m}{k}")
                 for n in _NUMS for l in "AB" for m in _NUMS for k in "AB"}


def bpm_score(native_a, native_b, shared):
    if not shared:
        return 0.0, None
    best = None
    best_score = -1.0
    for g in shared:
        pen = 0.5 * (bpm_grid.stretch_penalty(native_a, g)
                     + bpm_grid.stretch_penalty(native_b, g))
        s = 1.0 - 0.35 * pen           # shared grid => >= 0.65 by construction
        if s > best_score:
            best_score, best = s, g
    return best_score, best


def energy_continuity(analysis_a, segments_a, analysis_b, segments_b):
    """Compare A's outro-region energy to B's intro-region energy (O(1))."""
    pa = analysis_a["prefix"]["rms"]
    pb = analysis_b["prefix"]["rms"]
    out_seg = segments_a[-1]
    in_seg = segments_b[0]
    ea = window_mean(pa, out_seg["start_frame"], out_seg["end_frame"])
    eb = window_mean(pb, in_seg["start_frame"], in_seg["end_frame"])
    hi = max(ea, eb, 1e-9)
    return 1.0 - min(1.0, abs(ea - eb) / hi)


def match(track_a, track_b, analysis_a, segments_a, analysis_b, segments_b,
          grid_a, grid_b):
    """Score candidate B against current A. Returns dict w/ breakdown (P2-03).

    `track_a`/`track_b` are ``db.Track`` rows; `grid_a`/`grid_b` are the tracks'
    rendered grid BPMs.
    """
    shared = bpm_grid.shared_grid(grid_a, grid_b)
    s_bpm, best_grid = bpm_score(track_a.native_bpm, track_b.native_bpm, shared)
    s_key = CAMELOT_TABLE[(track_a.camelot, track_b.camelot)]
    s_energy = energy_continuity(analysis_a, segments_a, analysis_b, segments_b)
    total = (config.WEIGHT_BPM * s_bpm + config.WEIGHT_KEY * s_key
             + config.WEIGHT_ENERGY * s_energy)
    return {
        "track_id": track_b.id,
        "score": round(float(total), 4),
        "breakdown": {
            "bpm": round(float(s_bpm), 4),
            "key": round(float(s_key), 4),
            "energy": round(float(s_energy), 4),
            "weights": {"bpm": config.WEIGHT_BPM, "key": config.WEIGHT_KEY,
                        "energy": config.WEIGHT_ENERGY},
        },
        "shared_grid": shared,
        "best_grid_bpm": best_grid,
    }


def recommend(q, track_id, grids=None, limit=None):
    """Ranked candidates for `track_id` (P2-01..P2-05). ND (non-mixable)
    tracks are never candidates; candidates must share >=1 grid point and
    clear the score cutoff.

    `q` is a ``db.Queries`` scope. `grids` is the catalog-wide
    ``{track_id: [grid_bpm]}`` map; it is fetched in one query when not given.

    `limit` truncates the ranked list (default ``config.RECOMMENDATION_LIMIT``);
    pass 0 or a negative number for all of it. Truncation happens after the
    sort, so the cap changes how many of the best candidates are returned and
    never which ones — scoring still considers the whole catalog.
    """
    a = q.get_track(id=track_id)
    if not a or not a.mixable:
        return []
    if grids is None:
        grids = grid_bpms_by_track(q)
    an_a = a.analysis_json
    seg_a = a.segments_json
    grid_a = grids.get(track_id, [])

    out = []
    for b in q.list_mixable_tracks(mixable=True):
        if b.id == track_id:
            continue
        grid_b = grids.get(b.id, [])
        if not bpm_grid.shared_grid(grid_a, grid_b):
            continue                                            # P2-01
        m = match(a, b, an_a, seg_a, b.analysis_json, b.segments_json,
                  grid_a, grid_b)
        if m["score"] < config.MATCH_SCORE_CUTOFF:              # P2-04
            continue
        m["name"] = b.name
        m["artist"] = b.artist
        out.append(m)
    out.sort(key=lambda m: m["score"], reverse=True)            # P2-05
    if limit is None:
        limit = config.RECOMMENDATION_LIMIT
    return out[:limit] if limit and limit > 0 else out
