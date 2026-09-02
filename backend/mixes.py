"""Saved mixes: chain assembly, ripple edits, and the overlap invariant.

The persistence shape is deliberately minimal — ordering plus one gap per
track — because that is all a mix actually is once the audio is already
rendered. See docs/design-document.md §13.

Two representations:

  storage   `mix_tracks` rows: a singly-linked list (`next_id`) carrying
            `delta_beats`, the gap from the PREVIOUS track's start in whole
            beats at `grid_bpm`.
  transport what the API sends the client: the same chain flattened into an
            ordered list with seconds alongside beats, so the UI never has to
            know about node ids to draw a timeline.

Beats, not seconds, are the stored unit: an off-grid placement is then not
representable at all, rather than merely rejected by the UI.
"""
import time
import uuid

# A mix is a chain of pairwise transitions. Three tracks sounding at once is
# not a mix, it is a mistake — so a track may never overlap its
# second-nearest neighbour. Enforced on write, not just in the UI, because the
# API is reachable without it.
MAX_SIMULTANEOUS = 2


def new_id():
    return uuid.uuid4().hex


def beats_to_seconds(beats, grid_bpm):
    return (beats * 60.0) / grid_bpm if grid_bpm else 0.0


def seconds_to_beats(seconds, grid_bpm):
    return int(round((seconds * grid_bpm) / 60.0)) if grid_bpm else 0


class ChainError(ValueError):
    """The stored chain is malformed, or an edit would break an invariant."""


def walk(rows, head_id):
    """Order `mix_tracks` rows by following `next_id` from `head_id`.

    Guards the two ways a linked list in a database goes wrong: a cycle (walk
    forever) and an orphan (a row nothing points at). Both are reported rather
    than silently truncating a user's mix.
    """
    by_id = {r.id: r for r in rows}
    ordered, seen = [], set()
    node = head_id
    while node:
        if node in seen:
            raise ChainError(f"cycle in mix chain at node {node}")
        row = by_id.get(node)
        if row is None:
            raise ChainError(f"chain references missing node {node}")
        seen.add(node)
        ordered.append(row)
        node = row.next_id
    orphans = set(by_id) - seen
    if orphans:
        raise ChainError(f"{len(orphans)} mix_tracks row(s) unreachable from head")
    return ordered


def to_transport(ordered, durations):
    """Flatten an ordered chain into what the client draws.

    `durations` maps track_id -> seconds at that track's grid BPM.
    """
    out, start = [], 0.0
    for row in ordered:
        delta_s = beats_to_seconds(row.delta_beats, row.grid_bpm)
        start += delta_s
        out.append({
            "node_id": row.id,
            "track_id": row.track_id,
            "delta_beats": row.delta_beats,
            "delta_s": delta_s,
            "offset_s": start,
            "grid_bpm": row.grid_bpm,
            "duration_s": durations.get(row.track_id, 0.0),
        })
    return out


def check_overlaps(entries):
    """Reject a chain where any track overlaps a NON-neighbour.

    Neighbours overlapping is the whole point — that is the crossfade. A track
    reaching its second-nearest neighbour would put three tracks on the grid at
    once, which the playback engine and the crossfade model both assume cannot
    happen.
    """
    for i in range(len(entries) - MAX_SIMULTANEOUS):
        far = entries[i + MAX_SIMULTANEOUS]
        end_i = entries[i]["offset_s"] + entries[i]["duration_s"]
        if far["offset_s"] < end_i - 1e-6:
            raise ChainError(
                f"track {far['track_id']} would overlap {entries[i]['track_id']}, "
                f"putting {MAX_SIMULTANEOUS + 1} tracks on the grid at once")
    return entries


def min_delta_beats(entries, index):
    """Smallest legal `delta_beats` for the track at `index`.

    Derived from the same invariant as `check_overlaps`, so the UI can clamp a
    drag to exactly what the API would accept:

      * it may not start before its predecessor (that would reorder the mix);
      * it may not reach back into its second-nearest predecessor;
      * pulling it earlier drags its successor with it (rigid ripple), so the
        successor must not reach back into *its* second-nearest predecessor
        either.
    """
    if index <= 0:
        return 0
    grid = entries[index]["grid_bpm"]
    prev_start = entries[index - 1]["offset_s"]
    floor_s = prev_start

    if index >= MAX_SIMULTANEOUS:
        before = entries[index - MAX_SIMULTANEOUS]
        floor_s = max(floor_s, before["offset_s"] + before["duration_s"])

    nxt = entries[index + 1] if index + 1 < len(entries) else None
    if nxt is not None and index >= 1:
        prev = entries[index - 1]
        # successor_start = this_start + successor_delta  >=  prev_end
        floor_s = max(floor_s,
                      prev["offset_s"] + prev["duration_s"] - nxt["delta_s"])

    return max(0, seconds_to_beats(floor_s - prev_start, grid))


class MixRepository:
    """CRUD over saved mixes. All reads and writes go through the bounded
    database wrapper the caller supplies."""

    def __init__(self, database):
        self.database = database

    # -------------------------------------------------------------- reads
    def list(self):
        with self.database.reading() as q:
            mixes = q.list_mixes()
            counts = {}
            for m in mixes:
                counts[m.id] = len(q.list_mix_tracks(mix_id=m.id))
        return [{"id": m.id, "name": m.name, "track_count": counts.get(m.id, 0),
                 "updated_at": m.updated_at, "created_at": m.created_at}
                for m in mixes]

    def get(self, mix_id, durations):
        with self.database.reading() as q:
            m = q.get_mix(id=mix_id)
            if not m:
                return None
            rows = q.list_mix_tracks(mix_id=mix_id)
        entries = to_transport(walk(rows, m.head_id), durations)
        return {"id": m.id, "name": m.name, "created_at": m.created_at,
                "updated_at": m.updated_at, "tracks": entries}

    # ------------------------------------------------------------- writes
    def create(self, name="Untitled Mix", mix_id=None, now=None):
        now = now if now is not None else time.time()
        mix_id = mix_id or new_id()
        with self.database.writing() as q:
            q.upsert_mix(id=mix_id, name=name, head_id=None,
                         created_at=now, updated_at=now)
        return {"id": mix_id, "name": name, "track_count": 0,
                "created_at": now, "updated_at": now}

    def rename(self, mix_id, name, now=None):
        with self.database.writing() as q:
            q.rename_mix(id=mix_id, name=name,
                         updated_at=now if now is not None else time.time())

    def delete(self, mix_id):
        with self.database.writing() as q:
            q.delete_mix_tracks_for_mix(mix_id=mix_id)
            q.delete_mix(id=mix_id)

    def replace_chain(self, mix_id, tracks, durations, now=None):
        """Write the whole chain in one transaction.

        Used for structural edits (append, insert, delete, reorder) where the
        client already knows the resulting order. A single drag does NOT come
        through here — see `set_delta`, which is one row.
        """
        now = now if now is not None else time.time()
        entries, start = [], 0.0
        for t in tracks:
            delta_s = beats_to_seconds(t["delta_beats"], t["grid_bpm"])
            start += delta_s
            entries.append({**t, "delta_s": delta_s, "offset_s": start,
                            "duration_s": durations.get(t["track_id"], 0.0)})
        check_overlaps(entries)

        node_ids = [t.get("node_id") or new_id() for t in tracks]
        with self.database.writing() as q:
            q.delete_mix_tracks_for_mix(mix_id=mix_id)
            for i, t in enumerate(tracks):
                q.upsert_mix_track(
                    id=node_ids[i], mix_id=mix_id, track_id=t["track_id"],
                    next_id=node_ids[i + 1] if i + 1 < len(node_ids) else None,
                    delta_beats=int(t["delta_beats"]), grid_bpm=int(t["grid_bpm"]))
            q.set_mix_head(id=mix_id, head_id=node_ids[0] if node_ids else None,
                           updated_at=now)
        return node_ids

    def set_delta(self, mix_id, node_id, delta_beats, durations, now=None):
        """The ripple edit a track drag makes: ONE row, ONE column.

        Validated against the overlap invariant before it is written, so the
        API cannot be used to build a state the UI would refuse to draw.
        """
        now = now if now is not None else time.time()
        with self.database.reading() as q:
            m = q.get_mix(id=mix_id)
            if not m:
                return None
            rows = q.list_mix_tracks(mix_id=mix_id)

        ordered = walk(rows, m.head_id)
        index = next((i for i, r in enumerate(ordered) if r.id == node_id), None)
        if index is None:
            raise ChainError(f"node {node_id} is not in mix {mix_id}")

        proposed = [
            {"track_id": r.track_id, "delta_beats": int(delta_beats) if i == index
             else r.delta_beats, "grid_bpm": r.grid_bpm}
            for i, r in enumerate(ordered)
        ]
        entries, start = [], 0.0
        for t in proposed:
            delta_s = beats_to_seconds(t["delta_beats"], t["grid_bpm"])
            start += delta_s
            entries.append({**t, "delta_s": delta_s, "offset_s": start,
                            "duration_s": durations.get(t["track_id"], 0.0)})
        check_overlaps(entries)

        with self.database.writing() as q:
            q.set_mix_track_delta(id=node_id, delta_beats=int(delta_beats))
            q.touch_mix(id=mix_id, updated_at=now)
        return entries[index]
