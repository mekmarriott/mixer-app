"""Engines: connection lifecycle, transaction scoping and concurrency control.

An ``Engine`` hands out connections and knows how its backend handles
concurrency. Two exist:

``SQLiteEngine``
    Local and test use. Connections are per-thread (never shared across
    threads, unlike the previous ``check_same_thread=False`` global handle),
    the database runs in WAL mode so readers never block the writer, and a
    process-wide :class:`threading.RLock` serialises write transactions so two
    threads cannot collide on SQLite's single writer. ``busy_timeout`` covers
    writers in *other* processes (the benchmark, a second server).

``PostgresEngine``
    Deployment (Supabase). A psycopg connection pool provides real concurrency;
    MVCC and ``ON CONFLICT`` make the write lock unnecessary, so none is taken.

Transactions nest: a ``transaction()`` opened inside another joins the outer
one and commits once, at the outermost exit. That keeps a long ingestion loop
from holding the write lock while audio renders, and makes it safe for
instrumentation to write from inside a caller's transaction.

Retries live in :meth:`Engine.run`, which takes a callable — a context manager
cannot re-run its caller's block, so retrying has to own the unit of work.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import dialect

_SCHEMA_PATH = Path(__file__).parent / "sql" / "schema.sql"
_schema_cache = None


def schema_sql():
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = _SCHEMA_PATH.read_text()
    return _schema_cache


class DatabaseError(RuntimeError):
    """Configuration or connectivity problem in this layer."""


class _Scope(threading.local):
    """Per-thread record of the connection and transaction depth in force."""

    def __init__(self):
        self.conn = None
        self.depth = 0
        self.write = False


class Engine:
    dialect = None
    #: Retry budget for transient failures (lock contention, dropped sockets).
    max_retries = 4
    retry_base_delay = 0.02

    def __init__(self):
        self._scope = _Scope()

    # -- subclass hooks ----------------------------------------------------
    def _acquire(self):
        raise NotImplementedError

    def _release(self, conn):
        raise NotImplementedError

    def _is_retryable(self, exc):
        return False

    def _write_lock(self):
        return contextlib.nullcontext()

    # -- public API --------------------------------------------------------
    def migrate(self):
        """Apply schema.sql. Idempotent — every statement is IF NOT EXISTS.

        CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists,
        so a column added to schema.sql would never reach an existing catalog.
        Missing columns are therefore added after the tables exist — and
        *before* the indexes, since an index may be declared on a column the
        live table has not got yet.
        """
        statements = list(dialect.ddl_statements(schema_sql(), self.dialect))
        tables = [s for s in statements if "CREATE INDEX" not in s.upper()]
        indexes = [s for s in statements if "CREATE INDEX" in s.upper()]
        with self.connection(write=True) as conn:
            cur = conn.cursor()
            try:
                for stmt in tables:
                    cur.execute(stmt)
                self._add_missing_columns(cur)
                for stmt in indexes:
                    cur.execute(stmt)
            finally:
                cur.close()

    def _add_missing_columns(self, cur):
        for table, columns in dialect.declared_columns(schema_sql()).items():
            try:
                have = self._existing_columns(cur, table)
            except Exception:                  # table absent; DDL will have made it
                continue
            if not have:
                continue
            populated = None                   # counted lazily, at most once
            for name, decl in columns.items():
                if name in have:
                    continue
                # A NOT NULL column with no DEFAULT cannot be added to a table
                # that already has rows: there is no value to give them. This
                # is what a *rename* looks like from here (the old column is
                # still present, the new one is missing and NOT NULL), and a
                # rename needs a data migration, not a blind ADD COLUMN. Say so
                # instead of surfacing "Cannot add a NOT NULL column with
                # default value NULL" from the driver.
                needs_value = ("NOT NULL" in decl.upper()
                               and "DEFAULT" not in decl.upper())
                if needs_value:
                    if populated is None:
                        populated = cur.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if populated:
                        raise DatabaseError(
                            f"cannot migrate {table}.{name} automatically: it is "
                            f"declared NOT NULL with no DEFAULT and {table} "
                            f"already has {populated} row(s), so existing rows "
                            f"have no value for it. This is usually a renamed "
                            f"column and needs a hand-written migration that "
                            f"copies the old values across. Deleting the data "
                            f"directory and re-ingesting also resolves it.")
                rendered = dialect.render_ddl(decl, self.dialect)
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {rendered}")

    def _existing_columns(self, cur, table):
        raise NotImplementedError

    def table_columns(self, table):
        """Column names of a live table, in positional order.

        `SELECT * LIMIT 0` rather than PRAGMA or information_schema: it needs
        no dialect-specific SQL and returns the columns in the same order the
        row tuples will arrive in, which is the order that actually matters
        (see `verify_schema`).
        """
        with self.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT * FROM {table} LIMIT 0")   # noqa: S608
                return [d[0] for d in cur.description]
            finally:
                cur.close()

    @property
    def in_transaction(self):
        return self._scope.depth > 0

    @contextlib.contextmanager
    def connection(self, write=False):
        """Yield a connection inside a transaction; commit on clean exit,
        roll back on exception.

        Re-entrant: a nested call joins the enclosing transaction and neither
        commits nor releases the connection.

        A write scope may not be opened inside a read scope. The write lock is
        taken when the outermost scope opens, so escalating later would either
        skip it (racing other writers) or take it out of order (deadlock). The
        caller has to declare the outer scope a write scope instead.
        """
        scope = self._scope
        if scope.depth:
            if write and not scope.write:
                raise DatabaseError(
                    "cannot open a write scope inside a read scope — open the "
                    "outer scope with writing() instead")
            scope.depth += 1
            try:
                yield scope.conn
            finally:
                scope.depth -= 1
            return

        with (self._write_lock() if write else contextlib.nullcontext()):
            conn = self._acquire()
            scope.conn, scope.depth, scope.write = conn, 1, write
            try:
                yield conn
                conn.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
            finally:
                scope.conn, scope.depth, scope.write = None, 0, False
                self._release(conn)

    def run(self, fn, write=False):
        """Run ``fn(conn)`` in a transaction, retrying transient failures.

        The whole callable is re-executed on retry, so it must be idempotent —
        which the query set is, being upserts and reads.
        """
        if self.in_transaction:                 # already inside one: no retry
            with self.connection(write=write) as conn:
                return fn(conn)
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                with self.connection(write=write) as conn:
                    return fn(conn)
            except BaseException as exc:
                if attempt >= self.max_retries or not self._is_retryable(exc):
                    raise
                last = exc
                time.sleep(self.retry_base_delay * (2 ** attempt))
        raise DatabaseError("exhausted retries") from last     # pragma: no cover

    def dispose(self):
        pass


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

class SQLiteEngine(Engine):
    dialect = dialect.SQLITE

    def _existing_columns(self, cur, table):
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}

    def __init__(self, path, busy_timeout_s=10.0):
        super().__init__()
        self.path = str(path)
        self.busy_timeout_s = busy_timeout_s
        self._lock = threading.RLock()
        self._conns = threading.local()
        self._all_conns = []
        self._all_lock = threading.Lock()

    def _write_lock(self):
        return self._lock

    def _acquire(self):
        conn = getattr(self._conns, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_s,
                                   isolation_level="DEFERRED")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")    # readers don't block the writer
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=%d" % int(self.busy_timeout_s * 1000))
            conn.commit()
            self._conns.conn = conn
            with self._all_lock:
                self._all_conns.append(conn)
        return conn

    def _release(self, conn):
        pass                            # cached per thread, closed by dispose()

    def _is_retryable(self, exc):
        return (isinstance(exc, sqlite3.OperationalError)
                and "locked" in str(exc).lower())

    def dispose(self):
        with self._all_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()
        self._conns = threading.local()


# ---------------------------------------------------------------------------
# PostgreSQL / Supabase
# ---------------------------------------------------------------------------

class PostgresEngine(Engine):
    dialect = dialect.POSTGRES

    def _existing_columns(self, cur, table):
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s", (table,))
        return {row[0] for row in cur.fetchall()}

    #: Supabase's transaction pooler. Session state (including server-side
    #: prepared statements) does not survive between transactions on it, so
    #: psycopg's automatic prepare has to be switched off when talking to it.
    POOLER_PORT = 6543

    def __init__(self, url, min_size=None, max_size=8, prepare_threshold=None):
        super().__init__()
        try:
            import psycopg                                    # noqa: F401
            from psycopg_pool import ConnectionPool
        except ImportError as exc:                            # pragma: no cover
            raise DatabaseError(
                "PostgreSQL support needs psycopg — install the deployment "
                "extras:  pip install -r requirements-postgres.txt") from exc
        self.url = url
        self.pooled = uses_transaction_pooler(url)
        if min_size is None:
            # Behind a pooler (and on serverless generally) holding idle
            # connections open just consumes the pooler's budget.
            min_size = 0 if self.pooled else 1
        self._pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=True,
            kwargs={"autocommit": False, "prepare_threshold": prepare_threshold})

    def _acquire(self):
        return self._pool.getconn()

    def _release(self, conn):
        self._pool.putconn(conn)

    def _is_retryable(self, exc):
        import psycopg
        # 40001 serialization failure, 40P01 deadlock detected — the transaction
        # was fine, it just needs running again. OperationalError covers a
        # connection dropped by the pooler, which Supabase does routinely.
        if getattr(exc, "sqlstate", None) in ("40001", "40P01"):
            return True
        return isinstance(exc, psycopg.OperationalError)

    def dispose(self):
        self._pool.close()


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def uses_transaction_pooler(url):
    """True for a Supabase transaction-pooler URL (``…pooler.supabase.com:6543``).

    Deployments must point at the pooler rather than the direct connection: many
    short-lived connections would otherwise exhaust Postgres's direct limit.
    """
    parsed = urlparse(url)
    if parsed.port == PostgresEngine.POOLER_PORT:
        return True
    return "pooler." in (parsed.hostname or "")


def engine_from_url(url, **kwargs):
    """Build the engine named by a database URL.

      sqlite:///relative/path.sqlite3     sqlite:////absolute/path.sqlite3
      postgresql://user:pw@host:5432/db   postgres://...   (Supabase's form)
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ("sqlite", "sqlite3"):
        if parsed.netloc:
            raise DatabaseError(
                "sqlite URLs take a path, not a host: %r — use "
                "sqlite:///relative.db or sqlite:////absolute.db" % url)
        path = unquote(parsed.path)
        path = path[1:] if path.startswith("/") else path   # strip URL's own slash
        if not path or path == ":memory:":
            raise DatabaseError(
                "sqlite in-memory databases are not supported: connections are "
                "per-thread, so each thread would get its own empty database")
        return SQLiteEngine(path, **kwargs)
    if scheme in ("postgres", "postgresql"):
        return PostgresEngine(url, **kwargs)
    raise DatabaseError("unsupported database URL scheme: %r" % parsed.scheme)
