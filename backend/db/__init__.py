"""Database layer for the catalog.

Everything that reads or writes persistent state goes through here. The rest of
the backend never sees a driver connection, a cursor or a SQL string; it holds a
:class:`Database` and opens read or write scopes on it::

    from .db import Database

    database = Database.from_config().migrate()

    with database.reading() as q:
        track = q.get_track(id="1001")          # -> Track | None

    with database.writing() as q:
        q.upsert_variant(track_id="1001", grid_bpm=124, ...)

Layout
------
``sql/``        canonical schema + annotated queries — the source of truth
``codegen.py``  sqlc-style generator turning those .sql files into bindings
``models.py``   GENERATED row dataclasses
``queries.py``  GENERATED ``Queries`` — one typed method per named statement
``dialect.py``  canonical-type and placeholder translation (SQLite/PostgreSQL)
``engine.py``   connections, transaction scoping, concurrency control
``catalog.py``  ``Database``/``Catalog`` — the interface above

Swapping SQLite for Supabase is a URL change: set ``DJMIXER_DATABASE_URL`` to
the Postgres connection string. See ``docs/database.md``.
"""
from .catalog import Catalog, Database, DatabaseError
from .engine import Engine, PostgresEngine, SQLiteEngine, engine_from_url
from .models import (Latency, LatencySummaryRow, ListTrackSummariesRow, Track,
                     Variant)
from .queries import Queries

__all__ = [
    "Catalog", "Database", "DatabaseError",
    "Engine", "SQLiteEngine", "PostgresEngine", "engine_from_url",
    "Queries",
    "Track", "Variant", "Latency", "LatencySummaryRow", "ListTrackSummariesRow",
]
