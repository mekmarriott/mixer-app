"""Transition-point detection (Phase 3).

Two-tier: structural segments give the coarse *regions* to search; overlapping
beat-grid-aligned sliding windows generate the precise candidates within (and
straddling the edges of) those regions. All window aggregates come from prefix
sums — O(1) each, never re-scanning frames (P3-03).

window score = 0.35*energy_compat + 0.30*phase_alignment
             + 0.20*spectral_compat + 0.15*role_fit
"""
import numpy as np

from . import config
from .analysis import window_mean
from .segmentation import ENTRY_ROLES, EXIT_ROLES

W_ENERGY, W_PHASE, W_SPECTRAL, W_ROLE = 0.35, 0.30, 0.20, 0.15

# Grading a transition's LENGTH is a different question from grading its
# placement, so it uses its own weights over the same components. How well two
# windows blend decides how long they can be held together: a clean blend
# sustains a long fade, a rough one has to be got over with.
F_SPECTRAL, F_ENERGY, F_PHASE = 0.50, 0.30, 0.20

# Sections a track can be entered over — sparse enough to lay another track
# across. The contiguous run of them at B's entry is the room a fade has.
BLEND_ROLES = ("intro", "breakdown", "build")


def fade_bars(components):
    """Fade length in bars, graded from how well the two windows blend."""
    fitness = (F_SPECTRAL * components["spectral"]
               + F_ENERGY * components["energy"]
               + F_PHASE * components["phase"])
    fitness = min(1.0, max(0.0, fitness))
    ladder = config.FADE_BARS_LADDER
    return int(ladder[round(fitness * (len(ladder) - 1))])


def _entry_room_after(segments, start_frame, hop_dur):
    """Seconds of contiguous blendable material from `start_frame` onward.

    Follows the whole run of entry-role sections rather than just the one the
    fade starts in: an intro running into a breakdown is all room.
    """
    room = 0.0
    for seg in segments:
        if seg["end_frame"] <= start_frame:
            continue
        if seg["label"] not in BLEND_ROLES:
            break
        room += (seg["end_frame"] - max(seg["start_frame"], start_frame)) * hop_dur
    return room


def fade_for(candidate, grid_bpm, room_s=None):
    """Fade length for a candidate, stepped down the ladder to fit the room.

    Stepping down rather than clamping keeps the length on a musical count: a
    fade cut to fit would land on an arbitrary number of beats, where the rung
    below is still a whole number of bars.
    """
    bars = fade_bars(candidate["components"])
    if room_s is not None:
        bar_s = 4 * (60.0 / grid_bpm)
        allowed = room_s * config.FADE_ROOM_TOLERANCE
        fits = [b for b in config.FADE_BARS_LADDER
                if b <= bars and b * bar_s <= allowed]
        bars = max(fits) if fits else config.FADE_BARS_LADDER[0]
    return {"fade_bars": bars,
            "fade_s": round(bars * 4 * (60.0 / grid_bpm), 4)}


def _beats_per_frame(analysis):
    hop_dur = analysis["frames"]["hop_dur"]
    beat_dur = 60.0 / analysis["bpm"]
    return beat_dur / hop_dur


def _candidate_regions(segments, roles, top_k=3):
    scored = sorted(segments, key=lambda s: roles.get(s["label"], 0.0), reverse=True)
    return scored[:top_k]


def _window_starts(analysis, regions, window_frames, hop_frames, n_frames):
    """Beat-aligned window start frames within/straddling candidate regions.

    A window may run past the end of the track (the crossfade finishing as the
    track ends is the classic DJ exit) — aggregates are clamped to available
    frames at scoring time. Starts are capped so at least half a window of
    audio remains."""
    hop_dur = analysis["frames"]["hop_dur"]
    # Candidate starts snap to *downbeats* (bar boundaries): transitions in
    # 4/4 dance music start on the 1. Phase scoring then acts as a guard
    # rather than the discriminator.
    downbeat_frames = [b / hop_dur for b in analysis["beat_grid"][::4]]
    starts = set()
    for reg in regions:
        lo = max(0, reg["start_frame"] - window_frames // 2)     # straddle edges
        hi = min(n_frames - window_frames // 2, reg["end_frame"])
        prev = -1e9
        for bf in downbeat_frames:
            if lo <= bf <= hi and bf - prev >= hop_frames:
                # Snap to the *nearest* frame, not the one below: a downbeat
                # falling at 0.99 of a frame is a hair before the next frame,
                # not a whole frame after the previous one. Truncating leaves
                # up to a full frame of misalignment, rounding at most half.
                starts.add(int(round(bf)))
                prev = bf
    return sorted(starts)


def _phase_alignment(analysis, start_frame):
    """1.0 when the window starts on a downbeat (bar start), decaying with
    distance to the nearest downbeat, in beats."""
    hop_dur = analysis["frames"]["hop_dur"]
    t = start_frame * hop_dur
    beat_dur = 60.0 / analysis["bpm"]
    bar_dur = 4 * beat_dur
    grid = analysis["beat_grid"]
    if not grid:
        return 0.0
    downbeats = grid[::4]
    dist = min(abs(t - d) for d in downbeats)
    return max(0.0, 1.0 - (dist / bar_dur) * 2.0)


def _seg_role_at(segments, frame):
    for s in segments:
        if s["start_frame"] <= frame < s["end_frame"]:
            return s["label"]
    return segments[-1]["label"] if segments else "verse"


def score_pair(analysis_a, segments_a, analysis_b, segments_b):
    """Score all (A exit-window, B entry-window) candidates at the shared
    grid tempo. Returns the full scored curve + top markers (P3-05)."""
    bpf_a = _beats_per_frame(analysis_a)
    bpf_b = _beats_per_frame(analysis_b)
    win_a = int(config.WINDOW_BARS * 4 * bpf_a)
    win_b = int(config.WINDOW_BARS * 4 * bpf_b)
    hop_a = max(1, int(config.HOP_BARS * 4 * bpf_a))
    hop_b = max(1, int(config.HOP_BARS * 4 * bpf_b))
    n_a = len(analysis_a["frames"]["rms"])
    n_b = len(analysis_b["frames"]["rms"])

    regions_a = _candidate_regions(segments_a, EXIT_ROLES)
    regions_b = _candidate_regions(segments_b, ENTRY_ROLES)
    starts_a = _window_starts(analysis_a, regions_a, win_a, hop_a, n_a)
    starts_b = _window_starts(analysis_b, regions_b, win_b, hop_b, n_b)

    pa, pb = analysis_a["prefix"], analysis_b["prefix"]
    hop_dur_a = analysis_a["frames"]["hop_dur"]
    hop_dur_b = analysis_b["frames"]["hop_dur"]

    candidates = []
    for sa in starts_a:
        end_a = min(sa + win_a, n_a)                 # clamp: window may overrun
        ea = window_mean(pa["rms"], sa, end_a)
        ba = window_mean(pa["bass_ratio"], sa, end_a)
        pha = _phase_alignment(analysis_a, sa)
        role_a = EXIT_ROLES.get(_seg_role_at(segments_a, sa), 0.4)
        # Blend position into the role term: exits favor late in A,
        # entries favor early in B (see design doc §Transition scoring).
        pos_a = sa / max(1, n_a)
        fit_a = 0.6 * role_a + 0.4 * pos_a
        for sb in starts_b:
            end_b = min(sb + win_b, n_b)
            eb = window_mean(pb["rms"], sb, end_b)
            bb = window_mean(pb["bass_ratio"], sb, end_b)
            phb = _phase_alignment(analysis_b, sb)
            role_b = ENTRY_ROLES.get(_seg_role_at(segments_b, sb), 0.4)
            pos_b = 1.0 - sb / max(1, n_b)
            fit_b = 0.6 * role_b + 0.4 * pos_b

            hi = max(ea, eb, 1e-9)
            s_energy = 1.0 - min(1.0, abs(ea - eb) / hi)
            s_phase = 0.5 * (pha + phb)
            s_spec = 1.0 - min(1.0, (ba + bb))       # clashing bass penalized
            s_role = 0.5 * (fit_a + fit_b)
            score = (W_ENERGY * s_energy + W_PHASE * s_phase
                     + W_SPECTRAL * s_spec + W_ROLE * s_role)
            candidates.append({
                "a_start_s": round(sa * hop_dur_a, 4),
                "b_start_s": round(sb * hop_dur_b, 4),
                "score": round(float(score), 4),
                "components": {"energy": round(float(s_energy), 4),
                               "phase": round(float(s_phase), 4),
                               "spectral": round(float(s_spec), 4),
                               "role": round(float(s_role), 4)},
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    # Markers: top-N distinct A-side positions (what the UI draws, P4-20/21)
    markers, seen = [], set()
    for c in candidates:
        key = round(c["a_start_s"], 1)
        if key in seen:
            continue
        seen.add(key)
        markers.append(c)
        if len(markers) >= config.MARKER_TOP_N:
            break

    # Length is decided per marker, because it depends on how that particular
    # pair of windows blends and on how much room B's entry leaves.
    for m in markers:
        room = min(
            max(0.0, analysis_a["duration_s"] - m["a_start_s"]),
            _entry_room_after(segments_b,
                              int(round(m["b_start_s"] / hop_dur_b)), hop_dur_b))
        m.update(fade_for(m, analysis_a["bpm"], room_s=room))
        m["fade_room_s"] = round(float(room), 4)

    window_s = config.WINDOW_BARS * 4 * (60.0 / analysis_a["bpm"])
    return {
        "window_bars": config.WINDOW_BARS,
        "window_s": round(window_s, 4),
        "curve": candidates,          # full scored curve (P3-05)
        "markers": markers,
        "best": markers[0] if markers else None,
        "fade": markers[0]["fade_s"] if markers else None,
    }
