"""Shared fixed-BPM grid per genre bucket (Phase 1 step 6).

Two tracks are mix-compatible exactly when they share >=1 rendered grid BPM.
Variants are only rendered within the hard stretch cap."""
from . import config


def bucket_for(genre):
    if genre not in config.BPM_BUCKETS:
        raise ValueError(f"No BPM bucket for genre {genre!r}")
    return config.BPM_BUCKETS[genre]


#: Placeholder a catalog entry carries when its tempo band is not yet known.
#: Bulk discovery cannot fill `genre` in: the band is a *tempo* concept and the
#: tempo is not known until the audio has been analysed.
AUTO = "auto"


def resolve_bucket(genre, native_bpm):
    """The tempo band to grid a track on, deriving it if it was not curated.

    A curated band is kept — the shipped catalog recorded measured values and
    those stay authoritative. Anything else is derived from the analysed BPM,
    which is what `config.bucket_for_bpm` exists for.

    This matters more than it looks. `grid_points` filters the band's points by
    the stretch cap, so gridding a track against the WRONG band does not raise
    or warn: it silently returns an empty list, and the track is downloaded,
    analysed, stored, and left with no variants at all. Defaulting a bulk
    import to one band (the API metadata has no usable tempo, so `house` was
    the standing default) would do exactly that to every track outside
    120-128 BPM — most of any real catalog.

    Returns None for a BPM in no band, which correctly yields no variants.
    """
    if genre and genre != AUTO and genre in config.BPM_BUCKETS:
        return genre
    return config.bucket_for_bpm(native_bpm)


def grid_points(native_bpm, genre):
    """Grid BPMs this track can be rendered at, respecting MAX_STRETCH_RATIO."""
    if genre is None:                      # BPM outside every band
        return []
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
