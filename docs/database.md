# Database layer

Everything that reads or writes persistent state goes through `backend/db/`.
Nothing outside that package holds a driver connection, a cursor, or a SQL
string.

```
backend/db/
  sql/schema.sql          canonical schema — the single source of truth
  sql/queries/*.sql       annotated queries, sqlc-style
  codegen.py              generator: .sql -> models.py + queries.py
  models.py               GENERATED row dataclasses
  queries.py              GENERATED Queries — one typed method per statement
  dialect.py              canonical types + placeholders -> SQLite / PostgreSQL
  engine.py               connections, transaction scoping, concurrency control
  catalog.py              Database / Catalog — the interface the app uses
```

## Using it

```python
from backend.db import Database

database = Database.from_config().migrate()

with database.reading() as q:                 # no write lock; rolled back
    track = q.get_track(id="1001")            # -> Track | None
    grids = q.list_all_variants()

with database.writing() as q:                 # serialised; committed on exit
    q.upsert_variant(track_id="1001", grid_bpm=124,
                     ratio=1.0, path="/v.wav", duration_s=60.0)
```

Scopes **nest**: a scope opened inside another joins it and commits once, at the
outermost exit. That lets a helper open its own scope without committing a
caller's half-finished work or deadlocking on the write lock. Opening a *write*
scope inside a *read* scope raises — the write lock is taken when the outermost
scope opens, so escalating later would either skip it or take it out of order.

`Database.run(fn, write=True)` runs a callable in a scope and retries transient
failures (SQLite lock contention, Postgres serialization failures and dropped
pooler connections). Retrying re-executes the callable, which is why it takes a
function rather than being folded into the scope context managers.

## Changing the schema or a query

1. Edit `sql/schema.sql` or a file in `sql/queries/`.
2. Run `python -m backend.db.codegen`.
3. Commit the regenerated `models.py` and `queries.py` alongside the `.sql`.

`tests/backend/test_p5_db.py::TestCodegen::test_generated_files_are_not_stale`
fails if step 2 or 3 is skipped.

### Regenerating is not migrating — and a rename fails silently

`migrate()` only ever **creates**: every statement in `schema.sql` is
`IF NOT EXISTS`, so against a database that already exists it is a no-op. There
is no migration runner. Editing `schema.sql` changes what new databases get and
what the code expects; it does nothing to a database that is already out there.

That would be merely annoying if the mismatch produced an error. For one case it
does not. **Rows are decoded positionally** (see below), so column *order* is
load-bearing, and the three ways a live table can drift from the schema fail
very differently:

| Drift | What happens |
|---|---|
| Column **removed** | `IndexError` — loud, immediate |
| Column **appended** | Silently ignored (decoding enumerates the model's fields, not the row) |
| Column **renamed** | **Silently wrong** — the old column's value is handed to the new field |

A rename keeps the arity identical, so every value shifts into a
same-positioned field with a different name and nothing raises. Observed on
exactly this: after `audio_path` was renamed to `audio_key`, a database written
before the rename served `Track.audio_key == "/old/abs/path.wav"` and the API
answered `200` throughout.

This splits by **statement kind, not by engine**. Statements that name the
column (`UpsertTrack` lists its columns) fail loudly on both SQLite and
PostgreSQL; `SELECT *` mismaps silently on both, because psycopg also returns
tuples positionally. The signature of a stale deployment is therefore *writes
fail, reads lie* — and the loud write failure is actively misleading, because it
sends you to look at ingestion while reads quietly serve wrong values.

So, when a schema change reaches a database that already has rows:

- **Locally / in tests**, delete and rebuild. `data/` and `data-e2e/` are caches
  (note that `playwright.config.mjs` deliberately reuses `data-e2e/` between
  runs, so a stale one survives until you remove it).
- **Against a database whose contents matter**, write the `ALTER TABLE` by hand.
  Regenerating the bindings will not do it for you.

One SQLite-only trap with no PostgreSQL analogue: if `DB_PATH` moves,
`migrate()` cheerfully creates a *second* database file, and a stale catalog and
a fresh one coexist with nothing raising. When rows you know you ingested are
missing, check for more than one `.sqlite3` under `data/` before debugging
anything else.

### Annotation format

```sql
-- name: GetTrack :one
SELECT * FROM tracks WHERE id = :id;

-- name: LatencySummary :many
-- columns: stage TEXT, n INTEGER, mean_ms REAL, min_ms REAL, max_ms REAL
SELECT stage, COUNT(*) AS n, AVG(ms) AS mean_ms,
       MIN(ms) AS min_ms, MAX(ms) AS max_ms
FROM latency GROUP BY stage ORDER BY stage;
```

| Kind | Returns |
|---|---|
| `:one` | one row as a dataclass, or `None` |
| `:many` | a list of dataclasses |
| `:scalar` | the single selected column's value, or `None` |
| `:exec` | nothing |

`SELECT *` infers its row type from the schema. Any other select list needs a
`-- columns:` annotation naming each column and its canonical type, in select
order (it may wrap across several comment lines).

Result rows are decoded **positionally**: the row tuple is zipped against the
model's fields by index. That is what lets one code path serve sqlite3 and
psycopg without building a dict per row, and it is why the `-- columns:`
annotation has to list columns in *select* order. The cost is that column order
is load-bearing — see "Regenerating is not migrating" above for how a schema
that drifts out from under the models fails, and which of those failures is
silent. An explicit select list is safer than `SELECT *` in this respect: its
order is fixed by the query text rather than by the live table.

Parameters are named (`:track_id`) and their type is inferred from the column of
the same name on the table(s) the statement references. A parameter that matches
no column is a generation error, so a typo cannot silently bind NULL.

### Canonical column types

`schema.sql` is written in engine-neutral tokens:

| Token | SQLite | PostgreSQL | Python |
|---|---|---|---|
| `TEXT` | `TEXT` | `TEXT` | `str` |
| `INTEGER` | `INTEGER` | `INTEGER` | `int` |
| `REAL` | `REAL` | `DOUBLE PRECISION` | `float` |
| `BOOLEAN` | `INTEGER` | `BOOLEAN` | `bool` |
| `JSONDOC` | `TEXT` | `JSONB` | `dict`/`list` |
| `IDENTITY` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` | `int` |

Query *bodies* stay neutral — `ON CONFLICT … DO UPDATE` works on SQLite 3.24+
and PostgreSQL 9.5+ — so there is one set of `.sql` files, not one per engine.

## Local (default): SQLite

With `DJMIXER_DATABASE_URL` unset, the backend uses the file at
`config.DB_PATH` (`$DJMIXER_DATA/catalog.sqlite3`).

- One connection **per thread**, held only for the life of a request.
- `journal_mode=WAL` — readers never block the writer.
- `foreign_keys=ON` — deleting a track cascades to its variants.
- `busy_timeout` for writers in other processes (the benchmark, a second server).
- A process-wide `RLock` serialises write scopes, since SQLite has one writer.

This replaced a single `check_same_thread=False` connection shared by every
Flask worker thread, whose interleaved reads produced intermittent 500s and
phantom 404s (API-01 in `automation-test-manifest.md`).

## Deployment: Supabase / PostgreSQL

```bash
pip install -r requirements-postgres.txt
export DJMIXER_DATABASE_URL='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres'
```

Then `Database.from_config().migrate()` creates the schema with PostgreSQL
types. No application code changes.

**Use the transaction pooler (port 6543), not the direct connection.** Many
short-lived connections would exhaust Postgres's direct connection limit.
`PostgresEngine` detects a pooler URL and adapts: prepared statements are
disabled (the transaction pooler does not preserve session state between
transactions, so psycopg's automatic prepare would fail on a reused statement
name) and the pool's `min_size` drops to 0 so idle connections do not consume
the pooler's budget.

Concurrency control differs by design: no write lock is taken, because MVCC and
`ON CONFLICT` give PostgreSQL real concurrent writes. Serialization failures
(`40001`), deadlocks (`40P01`) and dropped connections are retried by
`Database.run`.

### Pushing the catalog to a remote database

Ingestion runs locally and the deployed app reads Supabase, so the rows have to
cross that gap:

```bash
make push-metadata-dry      # connect, verify the schema, write nothing
make push-metadata          # upsert tracks + variants
```

Both wrap `python -m backend.db.sync`, which reads `--from` (default: the
SQLite catalog under `$DJMIXER_DATA`, since that is where a local ingest run
accumulates) and writes `--to`, defaulting to the first of
`DJMIXER_REMOTE_DATABASE_URL`, `MIX_DB_POSTGRES_URL` or `POSTGRES_URL` that is
set — the last two being what the Supabase integration puts in the
environment.

Only `tracks` and `variants` move. `mixes`/`mix_tracks` belong to whichever
deployment created them and `latency` is local instrumentation, so neither is
copied. Audio objects are not uploaded either: the rows carry `audio_key` and
`object_key`, and putting the bytes behind those keys is the publisher's job.

The push **upserts and never prunes**. A locally deleted track stays in the
remote catalog, deliberately: a partially ingested local catalog is the normal
state (ingestion is resumable), and treating it as authoritative for deletion
would let one interrupted run empty production.

Do not point `SOURCE` at the local PostgreSQL without checking what is in it.
It is the development and test database, and the suites leave synthetic fixture
tracks (ids `1001`, `2001`, `9999`, ...) behind in it.

### Keeping the catalog and the blob store in agreement

Pushing metadata and publishing objects are separate steps, so they can drift.
`python -m backend.reconcile` (`make reconcile`) audits one against the other
and reports three kinds of disagreement:

| Kind | Meaning | Caught by ingestion? |
|---|---|---|
| absent | a row names an object the store has not got | yes — `already_done()` re-queues the track |
| **stale** | the object exists but is a **different render** | **no** |
| orphan | the store holds an object no row names | no |

The middle one is the dangerous case and the reason this exists. Re-analysing
a track can move its BPM, which moves its grid and changes every variant's
length; if the rows are republished and the objects are not, every key still
resolves, every duration is silently wrong, and mixes land off the beat. It
reads as a bug in the beat matcher, not as a stale upload.

`--apply` makes the store match the catalog — never the reverse. The rows are
internally consistent (`variant_duration × ratio == master_duration`) and the
render that produced each row is still on disk, so "upload the file the row
describes" has a source of truth behind it, whereas rewriting rows to match
whatever the store holds would bake a stale render into the metadata. Every
upload is guarded: where the local file does not itself match the row, the
entry is reported as unfixable rather than pushed, because the disagreement is
then between the catalog and local disk and only re-ingestion can settle it.
Orphans are deleted only with `--delete-orphans`, since that is the one
irreversible action here.

Durations are derived from object size rather than downloaded: everything is
mono 16-bit PCM behind a canonical 44-byte header, so `frames = (size − 44)/2`
exactly, and a whole-catalog audit costs one `list` call instead of one ranged
GET per object. `--verify-headers` re-reads the real headers to check that
assumption.

Two environment traps this ran into, both worth knowing before the first run:

- The Vercel Blob integration writes `BLOB_STORE_ID` into `.env`, and the CLI
  refuses to start when it is set without `VERCEL_OIDC_TOKEN` — the error names
  OIDC, which nothing in this project configures. `VercelBlobStore` drops the
  variable when authenticating with a read-write token.
- `VercelBlobStore(cli=...)` accepts a command list, so
  `["npx", "--yes", "vercel@latest"]` works without installing the CLI globally.

### Supabase's connection URL needs editing before libpq will take it

The URL Supabase hands out carries a vendor marker — `?supa=base-pooler.x`, and
`?pgbouncer=true` on the Prisma variant. Neither is a libpq parameter, and
libpq does not ignore what it does not recognise; it rejects the whole string
with `invalid URI query parameter: "supa"`. `engine.libpq_url()` strips those
markers on the way into `PostgresEngine`, so the value can be pasted into
`DJMIXER_DATABASE_URL` unedited. Nothing is lost: `pgbouncer=true` only tells
Prisma to stop preparing statements, which `PostgresEngine` already decides for
itself from the port.

One consequence of the pooler worth knowing when comparing values: it serves
`extra_float_digits = 0`, so PostgreSQL renders `DOUBLE PRECISION` as 15
significant digits and a float read back through it can differ from the one
written in the 16th. The *stored* value is bit-exact — `float8send()` proves
it — so this is a display artifact of the read path, not data loss.

### What is not verified here

The PostgreSQL path is covered through the dialect layer — `test_p5_db.py`
asserts the exact DDL and parameter style that would be sent — but the suite has
never run against a live server. Pool checkout, MVCC behaviour under contention,
and JSONB round-tripping through psycopg are untested. Run the backend suite
against a real Postgres before trusting the deployment path.
