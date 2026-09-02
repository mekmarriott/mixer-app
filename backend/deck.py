"""Zero-state deck: what to show before any track is chosen.

(Named `deck` rather than `catalog` to keep it distinct from
`backend/db/catalog.py`, which is the persistence-layer interface.)

At the opening screen there is no selected track, so there is nothing to match
against and no pair analysis to run. The deck is therefore a *browse* surface
rather than a ranked one: a handful of tracks per genre. Ranking only becomes
meaningful — and pair analysis only becomes necessary — once track 1 is picked.

Selection order, in priority:
  1. `popularity` descending, when the track row carries one.
  2. A deterministic shuffle seeded per genre, so the opening view is stable
     across requests and restarts but not simply alphabetical.

Jamendo does expose popularity (its /tracks endpoint accepts
`order=popularity_total`, and `include=stats` returns listen/download counts),
but nothing stores it today: `backend/jamendo.py` requests only
`include=licenses`, and the tracks table has no popularity column. Wiring that
up means touching both the Jamendo fetch and the schema, which are owned
elsewhere right now — so this reads `popularity` if present and falls back to
the shuffle when it is absent, and lights up on its own once the field exists.
"""
import hashlib

from . import config


def _stable_rank(genre, track_id):
    """Deterministic pseudo-random ordinal for (genre, track).

    Hash-based rather than `random.shuffle` so it needs no shared RNG state and
    gives the same answer in every worker thread and across restarts.
    """
    h = hashlib.sha256(f"{genre}:{track_id}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _sort_key(genre):
    def key(t):
        pop = t.get("popularity")
        # Negative so higher popularity sorts first; tracks without a value all
        # share bucket 0 and fall through to the stable shuffle.
        return (0 if pop is None else -float(pop), _stable_rank(genre, t["id"]))
    return key


def genre_groups(tracks, per_genre=None, include_unmixable=True):
    """Group tracks by genre and take the top `per_genre` from each.

    Genres are ordered by size (largest first), then by name, so the opening
    view leads with the deepest catalog. ND tracks are included by default —
    they are playable and must still be listed with attribution; the UI marks
    them unmixable rather than hiding them.
    """
    per_genre = per_genre or config.DECK_TRACKS_PER_GENRE

    buckets = {}
    for t in tracks:
        if not include_unmixable and not t.get("mixable", True):
            continue
        buckets.setdefault(t.get("genre") or "other", []).append(t)

    groups = []
    for genre, items in buckets.items():
        chosen = sorted(items, key=_sort_key(genre))[:per_genre]
        groups.append({
            "genre": genre,
            "total": len(items),
            "showing": len(chosen),
            "has_popularity": any(t.get("popularity") is not None for t in items),
            "tracks": chosen,
        })

    groups.sort(key=lambda g: (-g["total"], g["genre"]))
    return groups
