"""Shared fixed-BPM grid per genre bucket (Phase 1 step 6).

Two tracks are mix-compatible exactly when they share >=1 rendered grid BPM.
Variants are only rendered within the hard stretch cap."""
from . import config


def bucket_for(genre):
    if genre not in config.BPM_BUCKETS:
        raise ValueError(f"No BPM bucket for genre {genre!r}")
    return config.BPM_BUCKETS[genre]


def grid_points(native_bpm, genre):
    """Grid BPMs this track can be rendered at, respecting MAX_STRETCH_RATIO."""
    pts = []
    for g in bucket_for(genre):
        ratio = g / native_bpm
        if abs(ratio - 1.0) <= config.MAX_STRETCH_RATIO:
            pts.append(g)
    return pts


def shared_grid(points_a, points_b):
    return sorted(set(points_a) & set(points_b))


def stretch_penalty(native_bpm, grid_bpm):
    """0 (no stretch) .. 1 (at the cap). Larger stretch = more artifact risk."""
    return min(1.0, abs(grid_bpm / native_bpm - 1.0) / config.MAX_STRETCH_RATIO)
