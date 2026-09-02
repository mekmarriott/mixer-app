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
import time

from . import engine as engine_mod
from . import models
from . import status as status_mod
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
        self.verify_schema()
        return self

    def verify_schema(self):
        """Fail loudly when a live table's columns disagree with the models.

        `migrate()` only ever *creates* — every statement in schema.sql is
        IF NOT EXISTS — so against a database built by an older version it is a
        silent no-op and the old column layout survives. That would be merely
        annoying if it produced an error, but `_from_row` maps rows to
        dataclasses **positionally**, so a renamed column silently hands the
        old column's value to the new field. Nothing raises at any layer: the
        API answers 200, and the only symptom is wrong data at the far end.

        Measured on exactly that case (audio_path -> audio_key): /api/health,
        /api/tracks, /api/tracks/<id> all returned 200 and the audio endpoint
        returned a 302 to a nonsense URL built from a stale filesystem path.

        The three schema drifts are not equally dangerous, which is why only
        this one needed a guard:

          * a **renamed** column keeps the arity and shifts the values —
            silent, wrong, and undetectable from above;
          * a **removed** column shortens the row — IndexError, loud;
          * an **added** column is ignored, because the decode enumerates
            ``_FIELDS`` rather than the row.

        Only `tracks` and `variants` are checked, and the set is deliberately
        "models consumed by a ``SELECT *``" rather than "all models". Queries
        with an explicit select list (ListTrackSummaries, LatencySummary) take
        their column order from the SQL, so positional decoding cannot drift
        for them. `latency` in particular must NOT be added here: nothing does
        ``SELECT * FROM latency``, and a database created before the DB
        refactor has no `id` column on it, so checking it would fail a
        perfectly working install.

        This check does not migrate anything — there is no migration runner yet
        (docs/infrastructure-plan.md §11.1). It converts a silent wrong answer
        into an actionable startup error, which is the difference between
        deleting a cache directory and debugging the far end for an afternoon.
        """
        # No try/except around table_columns: migrate() has already run and
        # creates both tables, so a failure reading them is a real connection
        # or permission problem and should surface rather than be skipped.
        for table, model in (("tracks", models.Track), ("variants", models.Variant)):
            expected = [name for name, _ in model._FIELDS]
            actual = self.engine.table_columns(table)
            if actual != expected:
                raise DatabaseError(
                    f"schema mismatch on {table!r}: database has {actual}, "
                    f"code expects {expected}. migrate() only creates tables, "
                    f"it cannot alter them, and rows are mapped positionally — "
                    f"so continuing would silently return wrong values rather "
                    f"than fail. Delete the local data directory to rebuild "
                    f"(data/ and data-e2e/ are caches), or apply an "
                    f"ALTER TABLE against a database whose contents matter.")

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
        iterable of ``(grid_bpm, ratio, object_key, duration_s)``. One transaction, so
        a crash mid-render never leaves a track advertising variants that were
        not recorded.

        Ingestion calls this more than once per track — after fetch, then again
        after analysis — so fields the caller does not supply are left as they
        are rather than blanked (the upsert COALESCEs them).
        """
        with self.db.writing() as q:
            q.upsert_track(
                id=row["id"], name=row["name"], artist=row["artist"],
                genre=row["genre"], license=row["license"],
                license_nd=row["nd"], license_sa=row["sa"], license_nc=row["nc"],
                mixable=row["mixable"], native_bpm=row.get("native_bpm"),
                camelot=row.get("camelot"), duration_s=row.get("duration_s"),
                audio_key=row.get("audio_key"),
                analysis_json=row.get("analysis"),
                segments_json=row.get("segments"),
                status=row.get("status", status_mod.PENDING),
                status_error=row.get("status_error"),
                source_url=row.get("source_url"),
                fetched_at=row.get("fetched_at"),
                analyzed_at=row.get("analyzed_at"),
                ready_at=row.get("ready_at"))
            for grid_bpm, ratio, object_key, duration_s in variants:
                q.upsert_variant(track_id=row["id"], grid_bpm=grid_bpm,
                                 ratio=ratio, object_key=object_key,
                                 duration_s=duration_s)

    def advance_status(self, track_id, new_status, at=None):
        """Move a track's high-water mark forward and clear any stale error.

        Stamps the stage's timestamp in the same transaction, so a row can
        never claim a stage it has no time for.
        """
        with self.db.writing() as q:
            q.set_track_status(id=track_id, status=new_status)
            stamp = status_mod.STAMP.get(new_status)
            if stamp:
                when = time.time() if at is None else at
                getattr(q, f"stamp_track_{new_status}")(**{stamp: when,
                                                           "id": track_id})

    def mark_failed(self, track_id, error):
        """Record why ingestion stopped, WITHOUT rewinding the high-water mark:
        whatever was already fetched or analyzed stays valid for the retry."""
        with self.db.writing() as q:
            q.mark_track_failed(id=track_id, status_error=str(error))

    def status_of(self, track_id):
        with self.db.reading() as q:
            return q.get_track_status(id=track_id) or status_mod.PENDING

    def ingestion_state(self, configured_ids=None):
        """Per-track ingestion state, over the UNION of the config file and the
        database.

        Reports what the catalog endpoint must not: a track still being
        fetched, and one that failed and why.

        The union matters. config/tracks.json is a seed for local runs and
        service startup, not the definition of the catalog — a track published
        straight into the production database and blob store (which the
        serving endpoints are happy to serve, since they read only the
        database) would otherwise be invisible here, and a catalog containing
        it would still report `complete`. So each entry carries `in_config`:

          in_config=True,  row present     a seeded track, ingested
          in_config=True,  no row          seeded but not ingested yet
          in_config=False, row present     published directly, not seeded

        Only the first two are ingestion's responsibility; the third is
        reported so it is visible, not so it can be acted on.
        """
        with self.db.reading() as q:
            rows = {r.id: r for r in q.list_track_statuses()}
            variant_counts = {}
            for variant in q.list_all_variants():
                variant_counts[variant.track_id] = \
                    variant_counts.get(variant.track_id, 0) + 1

        if configured_ids is None:
            configured = set(rows)          # no config in play: everything counts
            ids = list(rows)
        else:
            configured = {str(i) for i in configured_ids}
            # Config order first (it is the human-meaningful order), then any
            # database row the config does not mention.
            ids = [str(i) for i in configured_ids]
            ids += [tid for tid in rows if tid not in configured]

        out = []
        for track_id in ids:
            row = rows.get(track_id)
            in_config = track_id in configured
            if row is None:
                out.append({"id": track_id, "status": status_mod.PENDING,
                            "error": None, "failed": False, "in_config": in_config,
                            "name": None, "artist": None, "mixable": False,
                            "variants": 0, "fetched_at": None,
                            "analyzed_at": None, "ready_at": None})
                continue
            out.append({
                "id": row.id, "status": row.status, "error": row.status_error,
                "failed": status_mod.is_failed(row), "in_config": in_config,
                "name": row.name,
                "artist": row.artist, "mixable": bool(row.mixable),
                "variants": variant_counts.get(row.id, 0),
                "fetched_at": row.fetched_at, "analyzed_at": row.analyzed_at,
                "ready_at": row.ready_at,
            })
        return out

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
