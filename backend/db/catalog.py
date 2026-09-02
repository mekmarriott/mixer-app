"""The database interface the rest of the backend talks to.

`Database` owns an engine and hands out :class:`~backend.db.queries.Queries`
scopes; `Catalog` adds the handful of domain operations that are more than one
statement or need shaping (an ingested track and its variants, the grid map
used by the catalog endpoint). Nothing outside this package touches a driver
connection or writes SQL.

Read and write scopes are explicit::

    with database.reading() as q:            # no write lock, rolled back
        track = q.get_track(track_id)

    with database.writing() as q:            # serialised, committed on exit
        q.upsert_track(...)

Both nest, so a caller already inside a write scope can call a helper that
opens its own without deadlocking or committing early.
"""
from __future__ import annotations

import contextlib

from . import engine as engine_mod
from .engine import DatabaseError                              # noqa: F401
from .models import (Latency, LatencySummaryRow,               # noqa: F401
                     ListTrackSummariesRow, Track, Variant)
from .queries import Queries


class Database:
    """Engine + query-scope factory."""

    def __init__(self, engine):
        self.engine = engine
        self.dialect = engine.dialect
        self._catalog = None

    # -- construction ------------------------------------------------------
    @classmethod
    def from_url(cls, url, **kwargs):
        return cls(engine_mod.engine_from_url(url, **kwargs))

    @classmethod
    def from_config(cls, **kwargs):
        """Build from configuration: ``DJMIXER_DATABASE_URL`` if set (Supabase
        gives you exactly such a URL), otherwise the local SQLite file."""
        from .. import config
        return cls.from_url(config.database_url(), **kwargs)

    def migrate(self):
        self.engine.migrate()
        return self

    def dispose(self):
        self.engine.dispose()

    # -- scopes ------------------------------------------------------------
    @contextlib.contextmanager
    def reading(self):
        with self.engine.connection(write=False) as conn:
            yield Queries(conn, self.dialect)

    @contextlib.contextmanager
    def writing(self):
        with self.engine.connection(write=True) as conn:
            yield Queries(conn, self.dialect)

    def run(self, fn, write=False):
        """Run ``fn(queries)`` in one scope, retrying transient failures.

        Use where the unit of work is a callable and can safely be repeated —
        the generated statements are reads and upserts, so they are.
        """
        return self.engine.run(lambda conn: fn(Queries(conn, self.dialect)),
                               write=write)

    @property
    def catalog(self):
        if self._catalog is None:
            self._catalog = Catalog(self)
        return self._catalog


class Catalog:
    """Domain operations over the track catalog."""

    def __init__(self, database):
        self.db = database

    # -- writes ------------------------------------------------------------
    def save_ingested_track(self, row, variants=()):
        """Persist an ingested track and its rendered variants atomically.

        `row` is the dict the ingestion pipeline assembles; `variants` is an
        iterable of ``(grid_bpm, ratio, path, duration_s)``. One transaction, so
        a crash mid-render never leaves a track advertising variants that were
        not recorded.
        """
        with self.db.writing() as q:
            q.upsert_track(
                id=row["id"], name=row["name"], artist=row["artist"],
                genre=row["genre"], license=row["license"],
                license_nd=row["nd"], license_sa=row["sa"], license_nc=row["nc"],
                mixable=row["mixable"], native_bpm=row.get("native_bpm"),
                camelot=row.get("camelot"), duration_s=row.get("duration_s"),
                audio_path=row.get("audio_path"),
                analysis_json=row.get("analysis"),
                segments_json=row.get("segments"))
            for grid_bpm, ratio, path, duration_s in variants:
                q.upsert_variant(track_id=row["id"], grid_bpm=grid_bpm,
                                 ratio=ratio, path=path, duration_s=duration_s)

    def record_latency(self, stage, track_id, ms, at):
        with self.db.writing() as q:
            q.insert_latency(stage=stage, track_id=track_id, ms=ms, at=at)

    def forget_track(self, track_id):
        """Remove a track and (by cascade) its variants."""
        with self.db.writing() as q:
            q.delete_track(id=track_id)

    # -- reads -------------------------------------------------------------
    def grid_bpms_by_track(self, q):
        return grid_bpms_by_track(q)


def grid_bpms_by_track(q):
    """``{track_id: [grid_bpm, ...]}`` for the whole catalog in one query.

    The catalog endpoint and the recommendation scan both need every track's
    grid; fetching them per track made either O(catalog) round trips.
    """
    grouped = {}
    for variant in q.list_all_variants():
        grouped.setdefault(variant.track_id, []).append(variant.grid_bpm)
    return grouped
