"""Admission control in front of the database.

`backend/db` fixed the *correctness* half of API-01: connections are per-thread
and never shared, so concurrent reads cannot interleave on one handle. This
module adds the other half — an explicit ceiling on how many requests may be
inside the database at once.

Why that is still needed after per-thread connections:

  * `SQLiteEngine` mints a connection per thread on demand. Nothing caps how
    many threads Flask runs, so nothing caps concurrent database work; a burst
    is bounded only by the worker pool.
  * `PostgresEngine` *is* bounded — by `psycopg_pool` (`max_size=8`) — but a
    caller that arrives with the pool full blocks inside `getconn()` until it
    times out, holding a worker thread. Blocking at admission instead makes the
    wait bounded, observable via `/api/status`, and answerable with a clean 503.

So the limit lives here, above the engine, and applies to whichever engine is
configured. It must stay STRICTLY BELOW the engine's connection ceiling — see
`connection_ceiling` — so an admitted caller never queues again downstream.

The gate is re-entrant per thread. `Database.reading()`/`writing()` nest (an
inner scope joins the outer transaction), and a nested scope must not try to
take a permit it is already holding, or a saturated gate would deadlock against
itself.
"""
import threading
from contextlib import contextmanager

from . import config


class AdmissionTimeout(RuntimeError):
    """The database is saturated; the caller should shed load (HTTP 503)
    rather than queue without bound."""


def connection_ceiling(database):
    """How many connections the configured engine can have open at once.

    Postgres reports its pool size. SQLite has no intrinsic ceiling — it makes
    one connection per thread — so it reports None and the configured admission
    limit becomes the effective bound.
    """
    engine = getattr(database, "engine", None)
    pool = getattr(engine, "_pool", None)
    size = getattr(pool, "max_size", None)
    return int(size) if size else None


class BoundedDatabase:
    """`Database` wrapper that admits at most `max_concurrency` callers.

    Delegates everything else, so it is a drop-in for `Database`.
    """

    def __init__(self, database, max_concurrency=None, timeout=None):
        self._db = database
        self.max_concurrency = max_concurrency or config.DB_MAX_CONCURRENCY
        self.timeout = timeout if timeout is not None else config.DB_ACQUIRE_TIMEOUT_S

        ceiling = connection_ceiling(database)
        if ceiling is not None and self.max_concurrency >= ceiling:
            raise ValueError(
                f"max_concurrency ({self.max_concurrency}) must stay strictly below "
                f"the engine's connection ceiling ({ceiling}), so admission — not "
                f"connection checkout — is where callers queue")

        self._gate = threading.BoundedSemaphore(self.max_concurrency)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._in_flight = 0
        self.stats = {"admitted": 0, "timeouts": 0, "peak_in_flight": 0}

    # ---------------------------------------------------------------- gate
    @contextmanager
    def _admit(self):
        # Re-entrant: a nested scope on this thread already holds the permit.
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        if not self._gate.acquire(timeout=self.timeout):
            with self._lock:
                self.stats["timeouts"] += 1
            raise AdmissionTimeout(
                f"waited {self.timeout}s for a database slot "
                f"(limit {self.max_concurrency})")
        self._local.depth = 1
        with self._lock:
            self._in_flight += 1
            self.stats["admitted"] += 1
            self.stats["peak_in_flight"] = max(self.stats["peak_in_flight"],
                                               self._in_flight)
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
            self._local.depth = 0
            self._gate.release()

    def snapshot(self):
        with self._lock:
            return {**self.stats, "in_flight": self._in_flight,
                    "max_concurrency": self.max_concurrency,
                    "connection_ceiling": connection_ceiling(self._db)}

    # ------------------------------------------------------------ delegation
    @contextmanager
    def reading(self):
        with self._admit(), self._db.reading() as q:
            yield q

    @contextmanager
    def writing(self):
        with self._admit(), self._db.writing() as q:
            yield q

    def run(self, fn, write=False):
        with self._admit():
            return self._db.run(fn, write=write)

    @property
    def catalog(self):
        # Catalog opens its own scopes on the wrapped Database; route it here
        # so its work is admitted too.
        return _BoundedCatalog(self, self._db.catalog)

    @property
    def engine(self):
        return self._db.engine

    def migrate(self):
        self._db.migrate()
        return self

    def dispose(self):
        return self._db.dispose()


class _BoundedCatalog:
    """Passes Catalog calls through the admission gate."""

    def __init__(self, guard, catalog):
        self._guard = guard
        self._catalog = catalog

    def __getattr__(self, name):
        attr = getattr(self._catalog, name)
        if not callable(attr):
            return attr

        def admitted(*a, **kw):
            with self._guard._admit():
                return attr(*a, **kw)
        return admitted
