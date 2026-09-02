"""Copy catalog metadata from one database to another.

Ingestion runs locally — it needs ffmpeg, rubberband and an hour of CPU per
handful of tracks (see backend/publish.py) — but the deployed app reads its
catalog from Supabase. Audio crosses that gap as blobs; the *rows* have to
cross it too, and nothing else did that. This is that half: the `tracks` and
`variants` tables, read from the local catalog and upserted into the remote
one.

    python -m backend.db.sync --from sqlite:///data/catalog.sqlite3 --dry-run
    python -m backend.db.sync --from sqlite:///data/catalog.sqlite3

Only metadata moves. `mixes`/`mix_tracks` are user-created state that belongs
to whichever deployment produced it, and `latency` is local instrumentation;
copying either into production would be publishing local noise, so both are
left alone. Objects are not uploaded either — `audio_key` and `object_key`
name blobs in the store, and getting those there is the publisher's job.

## Why upsert rather than replace

The same reason ingestion upserts (see sql/queries/tracks.sql): `INSERT OR
REPLACE` would delete the track row first and cascade away its variants. This
is also what makes the command safe to re-run after a half-finished network
blip, which — pushing to a remote database over a transaction pooler — is the
expected failure, not the exceptional one.

The corollary is that a row deleted locally is **not** deleted remotely. A
push adds and updates; it never prunes. That is deliberate: a partially
ingested local catalog is normal (ingestion is resumable), and treating it as
authoritative for deletion would let one interrupted run empty production.

## Batching

Rows go over in chunks inside their own transactions rather than one large
one. A transaction pooler is not a place to hold a long-running transaction —
it is shared, and a 10k-row single transaction would pin a server-side
connection for the duration — and `Database.run` can only retry a dropped
connection if the unit of work is small enough to be worth repeating.
"""
from __future__ import annotations

import argparse
import os
import sys

from .. import config
from . import catalog as catalog_mod
from .engine import DatabaseError

#: Where the remote URL comes from when `--to` is not given, in order. The
#: DJMIXER_ name is the one this project documents; the rest are what the
#: Supabase integration writes into the environment (`MIX_DB_` is this
#: project's Vercel prefix), accepted so a pulled environment works unedited.
TARGET_URL_VARS = (
    "DJMIXER_REMOTE_DATABASE_URL",
    "MIX_DB_POSTGRES_URL",
    "POSTGRES_URL",
)

#: Rows per transaction. Small enough that a retry after a dropped pooler
#: connection is cheap, large enough that 10k tracks is not 10k round trips.
CHUNK = 200


def resolve_target(explicit=None):
    """The remote database URL, from `--to` or the environment."""
    if explicit:
        return explicit
    for var in TARGET_URL_VARS:
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    raise DatabaseError(
        "no target database. Pass --to, or set one of: %s"
        % ", ".join(TARGET_URL_VARS))


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def read_metadata(source):
    """Every track and variant row in `source`."""
    with source.reading() as q:
        return q.list_tracks(), q.list_all_variants()


def write_metadata(target, tracks, variants, progress=None):
    """Upsert `tracks` then `variants` into `target`, chunk by chunk.

    Tracks go first: `variants.track_id` is a foreign key, so a variant whose
    track has not landed yet would be rejected.
    """
    done = 0
    for chunk in _chunks(tracks, CHUNK):
        def put(q, chunk=chunk):
            for t in chunk:
                q.upsert_track(
                    id=t.id, name=t.name, artist=t.artist, genre=t.genre,
                    license=t.license, license_nd=t.license_nd,
                    license_sa=t.license_sa, license_nc=t.license_nc,
                    mixable=t.mixable, native_bpm=t.native_bpm,
                    camelot=t.camelot, duration_s=t.duration_s,
                    audio_key=t.audio_key, analysis_json=t.analysis_json,
                    segments_json=t.segments_json, status=t.status,
                    status_error=t.status_error, source_url=t.source_url,
                    fetched_at=t.fetched_at, analyzed_at=t.analyzed_at,
                    ready_at=t.ready_at)
        target.run(put, write=True)
        done += len(chunk)
        if progress:
            progress("  tracks   %d/%d" % (done, len(tracks)))

    done = 0
    for chunk in _chunks(variants, CHUNK):
        def put(q, chunk=chunk):
            for v in chunk:
                q.upsert_variant(track_id=v.track_id, grid_bpm=v.grid_bpm,
                                 ratio=v.ratio, object_key=v.object_key,
                                 duration_s=v.duration_s)
        target.run(put, write=True)
        done += len(chunk)
        if progress:
            progress("  variants %d/%d" % (done, len(variants)))


def sync(source_url, target_url, dry_run=False, progress=print):
    """Push catalog metadata from `source_url` to `target_url`.

    Returns a summary dict. With `dry_run`, the target is opened and migrated
    but nothing is written — enough to prove the credentials and the schema are
    good before committing to the rows.
    """
    if source_url == target_url:
        raise DatabaseError(
            "source and target are the same database: %r" % source_url)

    source = catalog_mod.Database.from_url(source_url)
    try:
        tracks, variants = read_metadata(source)
    finally:
        source.dispose()

    progress("source   : %s" % source_url)
    progress("target   : %s" % redact_url(target_url))
    progress("to push  : %d tracks, %d variants"
             % (len(tracks), len(variants)))

    target = catalog_mod.Database.from_url(target_url)
    try:
        # migrate() is create-only and then verifies the live columns against
        # the models, which is the check that matters here: pushing into a
        # database whose schema drifted would write through positionally
        # decoded rows. See docs/database.md.
        target.migrate()
        if dry_run:
            progress("dry run  : schema verified, nothing written")
        else:
            write_metadata(target, tracks, variants, progress=progress)
        with target.reading() as q:
            remote_tracks = len(q.list_tracks())
            remote_variants = len(q.list_all_variants())
    finally:
        target.dispose()

    progress("remote   : %d tracks, %d variants"
             % (remote_tracks, remote_variants))
    return {"pushed_tracks": 0 if dry_run else len(tracks),
            "pushed_variants": 0 if dry_run else len(variants),
            "remote_tracks": remote_tracks,
            "remote_variants": remote_variants}


def redact_url(url):
    """A connection URL with its password removed, for printing."""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if not p.password:
        return url
    netloc = "%s:***@%s" % (p.username, p.hostname)
    if p.port:
        netloc += ":%d" % p.port
    return urlunparse(p._replace(netloc=netloc))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m backend.db.sync",
        description="Push catalog metadata (tracks and variants) to a remote "
                    "database. Audio objects are not uploaded.")
    ap.add_argument("--from", dest="source", default=None, metavar="URL",
                    help="source database URL (default: the configured local "
                         "one, %s)" % config.database_url())
    ap.add_argument("--to", dest="target", default=None, metavar="URL",
                    help="target database URL (default: the first of %s that "
                         "is set)" % ", ".join(TARGET_URL_VARS))
    ap.add_argument("--dry-run", action="store_true",
                    help="connect and verify the schema, write nothing")
    args = ap.parse_args(argv)

    try:
        result = sync(args.source or config.database_url(),
                      resolve_target(args.target),
                      dry_run=args.dry_run)
    except DatabaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0 if result["remote_tracks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
