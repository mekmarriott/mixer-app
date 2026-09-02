"""Matching & recommendation (Phase 2).

score = W_KEY * key_score + W_ENERGY * energy_score + W_STRETCH * stretch_score

Tempo is settled before scoring starts: sharing a grid point is a hard gate,
so a tempo term can only separate survivors by how far each is stretched, and
the weight belongs on what the gate has not already decided.

stretch_score how little the CANDIDATE is stretched onto the shared grid.
key_score     Camelot-wheel lookup (24x24, precomputed).
energy_score  outro-of-A vs intro-of-B mean RMS closeness (O(1) via prefix sums).
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


def stretch_score(native_a, native_b, shared):
    """How little the CANDIDATE has to be stretched, and the grid to do it on.

    The grid point is chosen for the pair — the sum of both penalties — but the
    score is the candidate's penalty alone. The seed's is the same for every
    candidate in a call, so including it only adds a constant, and averaging
    the two is what compressed this term into the narrow band that made it
    useless: a shared grid already guarantees both sides are within
    MAX_STRETCH_RATIO, so the average could never fall far. Scored alone, the
    term uses its whole range.
    """
    if not shared:
        return 0.0, None
    best = min(shared, key=lambda g: (bpm_grid.stretch_penalty(native_a, g)
                                      + bpm_grid.stretch_penalty(native_b, g)))
    return 1.0 - bpm_grid.stretch_penalty(native_b, best), best


def region_energies(analysis, segments):
    """`(outro, intro)` mean energy for the regions a transition joins.

    The outro is the last segment — what a mix fades out of — and the intro is
    the first, what it fades into. These two numbers are all that scoring ever
    wanted from the analysis and segment blobs, which is why they are stored on
    the track row (schema.sql) instead of being rederived per comparison.
    """
    prefix = analysis["prefix"]["rms"]
    out_seg = segments[-1]
    in_seg = segments[0]
    return (window_mean(prefix, out_seg["start_frame"], out_seg["end_frame"]),
            window_mean(prefix, in_seg["start_frame"], in_seg["end_frame"]))


def energy_score(outro_a, intro_b):
    """How well A's outro level meets B's intro level, on 0..1."""
    hi = max(outro_a, intro_b, 1e-9)
    return 1.0 - min(1.0, abs(outro_a - intro_b) / hi)


def energy_continuity(analysis_a, segments_a, analysis_b, segments_b):
    """Compare A's outro-region energy to B's intro-region energy (O(1))."""
    ea = region_energies(analysis_a, segments_a)[0]
    eb = region_energies(analysis_b, segments_b)[1]
    return energy_score(ea, eb)


def match(track_a, track_b, analysis_a, segments_a, analysis_b, segments_b,
          grid_a, grid_b):
    """Score candidate B against current A. Returns dict w/ breakdown (P2-03).

    `track_a`/`track_b` are ``db.Track`` rows; `grid_a`/`grid_b` are the tracks'
    rendered grid BPMs.
    """
    shared = bpm_grid.shared_grid(grid_a, grid_b)
    s_stretch, best_grid = stretch_score(track_a.native_bpm, track_b.native_bpm,
                                        shared)
    s_key = CAMELOT_TABLE[(track_a.camelot, track_b.camelot)]
    s_energy = energy_continuity(analysis_a, segments_a, analysis_b, segments_b)
    total = (config.WEIGHT_KEY * s_key + config.WEIGHT_ENERGY * s_energy
             + config.WEIGHT_STRETCH * s_stretch)
    return {
        "track_id": track_b.id,
        "score": round(float(total), 4),
        "breakdown": {
            "stretch": round(float(s_stretch), 4),
            "key": round(float(s_key), 4),
            "energy": round(float(s_energy), 4),
            "weights": {"stretch": config.WEIGHT_STRETCH,
                        "key": config.WEIGHT_KEY,
                        "energy": config.WEIGHT_ENERGY},
        },
        "shared_grid": shared,
        "best_grid_bpm": best_grid,
    }


def recommend(q, track_id, grids=None, limit=None, rows=None):
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
    # The summary carries every column scoring reads. get_track would carry the
    # analysis and segment blobs too — a megabyte-scale read for a row whose
    # tempo, key and stored energies are all that matter here.
    a = q.get_track_summary(id=track_id)
    if not a or not a.mixable:
        return []
    if grids is None:
        grids = grid_bpms_by_track(q)
    if rows is None:
        rows = q.list_track_summaries()
    grid_a = grids.get(track_id, [])
    if limit is None:
        limit = config.RECOMMENDATION_LIMIT

    # A's outro is the only thing about A that scoring needs.
    outro_a = a.outro_energy
    if outro_a is None:
        from . import analysis_store
        an_a = analysis_store.hydrate(track_id, q.get_track_analysis(id=track_id))
        seg_a = q.get_track_segments(id=track_id)
        if not an_a or not seg_a:
            return []
        outro_a = region_energies(an_a, seg_a)[0]

    # One pass over summaries, reading no blobs.
    #
    # This used to walk `list_mixable_tracks`, which is SELECT *, so ranking a
    # single track dragged analysis_json and segments_json for the WHOLE
    # catalog across the wire — megabytes apiece — to reduce each to one O(1)
    # number. The cost grew with the catalog until the request hit the 30
    # second function ceiling, and since a timed-out request keeps running,
    # reloading multiplied the load instead of retrying it: the instance
    # saturated and even GET / began to time out.
    #
    # All three score terms now come from columns that ListTrackSummaries
    # already carries, and the BPM grid check rejects an incompatible candidate
    # before any of them is computed.
    out = []
    for b in rows:
        if b.id == track_id or not b.mixable:
            continue                                            # P2-01
        grid_b = grids.get(b.id, [])
        shared = bpm_grid.shared_grid(grid_a, grid_b)
        if not shared:
            continue                                            # P2-01
        s_stretch, best_grid = stretch_score(a.native_bpm, b.native_bpm, shared)
        s_key = CAMELOT_TABLE[(a.camelot, b.camelot)]

        intro_b = b.intro_energy
        if intro_b is None:
            # Pre-dates the stored columns, or was ingested from an analysis
            # this could not read. Rare and self-healing on re-ingest, so it is
            # worth one narrow read rather than dropping the candidate.
            from . import analysis_store
            an_b = analysis_store.hydrate(b.id, q.get_track_analysis(id=b.id))
            seg_b = q.get_track_segments(id=b.id)
            if not an_b or not seg_b:
                continue
            intro_b = region_energies(an_b, seg_b)[1]

        s_energy = energy_score(outro_a, intro_b)
        total = (config.WEIGHT_KEY * s_key + config.WEIGHT_ENERGY * s_energy
                 + config.WEIGHT_STRETCH * s_stretch)
        if total < config.MATCH_SCORE_CUTOFF:                   # P2-04
            continue
        out.append({
            "track_id": b.id,
            "score": round(float(total), 4),
            "breakdown": {
                "stretch": round(float(s_stretch), 4),
                "key": round(float(s_key), 4),
                "energy": round(float(s_energy), 4),
                "weights": {"stretch": config.WEIGHT_STRETCH,
                            "key": config.WEIGHT_KEY,
                            "energy": config.WEIGHT_ENERGY},
            },
            "shared_grid": shared,
            "best_grid_bpm": best_grid,
            "name": b.name,
            "artist": b.artist,
        })

    out.sort(key=lambda m: m["score"], reverse=True)            # P2-05
    return out[:limit] if limit and limit > 0 else out
