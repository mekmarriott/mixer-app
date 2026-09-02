"""Blob storage provider seam (deployment step 1).

Audio lives outside the database and outside the application server. This
module is the only place that knows where. Two backends implement one
interface:

  local   -- files under DATA_DIR, served by Flask at /blobs/<key>. This is
             the development and test backend; it needs no network, no
             credentials and no cloud account, so the whole ingest -> store ->
             serve -> play path is exercised by the normal test suite.
  vercel  -- Vercel Blob, via the `vercel blob` CLI. Used by the batch
             publisher when uploading a real catalog.

The important asymmetry: **the request-serving path never calls a backend's
upload code, and in deployment never calls this module at all.** `audio_key`
is recorded at ingest time, `url_for()` turns it into an absolute URL, and the
API answers with a 302. Audio bytes never pass through the API process, which
is what keeps a serverless function from being billed for — and held open
during — every track download (docs/infrastructure-plan.md §1.2).

Keys are provider-agnostic and stable:

    audio/<track_id>.<ext>                 master
    variants/<track_id>_<grid_bpm>.<ext>   rendered tempo variant
    analysis/<track_id>.npz                frame/prefix arrays (future)

Because only the key is persisted, moving providers (Vercel Blob -> Cloudflare
R2, per the cost analysis in docs/infrastructure-plan.md §4.2) is a bucket copy
plus a change of environment variable, not a data migration.
"""
from __future__ import annotations

import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config


class BlobStoreError(RuntimeError):
    pass


def master_key(track_id, ext="wav"):
    return f"audio/{track_id}.{ext}"


def variant_key(track_id, grid_bpm, ext="wav"):
    return f"variants/{track_id}_{int(grid_bpm)}.{ext}"


def _content_type(key):
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


class BlobStore:
    """Interface. `put*` returns the key; `url_for` resolves it for a client."""

    def put_file(self, key, src_path, content_type=None):
        raise NotImplementedError

    def put_bytes(self, key, data, content_type=None):
        raise NotImplementedError

    def url_for(self, key):
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def local_path(self, key):
        """Filesystem path if this backend has one, else None. Lets the
        ingestion pipeline write a rendered variant straight to its final
        location instead of writing to a temp file and copying."""
        return None


class LocalBlobStore(BlobStore):
    """Filesystem-backed store — the local/test double for a cloud bucket.

    Keys map onto `root/<key>`, which deliberately reproduces the historical
    data/ layout (`data/audio/<id>.wav`, `data/variants/<id>_<bpm>.wav`) so
    that switching to key-addressed storage did not move any file on disk.
    """

    def __init__(self, root=None, base_url="/blobs"):
        self._root = root
        self.base_url = base_url.rstrip("/")

    @property
    def root(self):
        """Resolved on each access rather than captured in __init__.

        `get_store()` caches a process-wide instance, while config.DATA_DIR is
        reassigned at runtime — the test fixture points it at a temp directory,
        and DJMIXER_DATA overrides it per invocation. A root captured at
        construction time silently keeps writing to wherever the first caller
        happened to be, which is how variants end up somewhere the rest of the
        pipeline is not looking.
        """
        return Path(self._root) if self._root is not None else Path(config.DATA_DIR)

    def _path(self, key):
        p = (self.root / key).resolve()
        root = self.root.resolve()
        # Keys come from ingestion, not from requests, but a store that can be
        # walked out of with "../" is the kind of thing that becomes a request
        # path later. Refuse it here rather than rely on callers.
        if not str(p).startswith(str(root) + os.sep):
            raise BlobStoreError(f"key escapes store root: {key!r}")
        return p

    def local_path(self, key):
        return self._path(key)

    def put_file(self, key, src_path, content_type=None):
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(src_path).resolve() != dst:
            shutil.copyfile(src_path, dst)
        return key

    def put_bytes(self, key, data, content_type=None):
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return key

    def url_for(self, key):
        return f"{self.base_url}/{key}"

    def exists(self, key):
        return self._path(key).is_file()


class VercelBlobStore(BlobStore):
    """Vercel Blob via the `vercel blob` CLI.

    The CLI rather than the REST API because Vercel documents the CLI and does
    not document a REST contract for uploads — @vercel/blob is a JS SDK and
    this backend is Python. That is an acceptable trade here because uploads
    only ever happen in the batch publisher on a developer machine, never in a
    request path, so the subprocess cost and the Node dependency stay off the
    server entirely.

    `vercel blob put` prints the resulting URL on stdout. That URL is absolute
    and public, so it is what gets persisted and handed to clients.
    """

    URL_RE = re.compile(r"https://\S+\.blob\.vercel-storage\.com/\S+")

    def __init__(self, token=None, base_url=None, cli="vercel"):
        self.token = token or os.environ.get("BLOB_READ_WRITE_TOKEN")
        self.base_url = (base_url or os.environ.get("BLOB_BASE_URL") or "").rstrip("/")
        self.cli = cli
        self._urls = {}

    def _run(self, args):
        if not shutil.which(self.cli):
            raise BlobStoreError(
                f"{self.cli!r} not on PATH — the Vercel CLI is required to "
                "upload to Vercel Blob (npm i -g vercel)")
        proc = subprocess.run([self.cli, "blob", *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise BlobStoreError(
                f"vercel blob {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def put_file(self, key, src_path, content_type=None):
        args = [
            "put", str(src_path),
            "--pathname", key,
            "--access", "public",
            "--content-type", content_type or _content_type(key),
            "--allow-overwrite",
            # Immutable content addressed by a stable key: cache hard. Without
            # this, audio re-fetches on every play defeat the CDN.
            "--cache-control-max-age", "31536000",
        ]
        if self.token:
            args += ["--rw-token", self.token]
        out = self._run(args)
        m = self.URL_RE.search(out)
        if not m:
            raise BlobStoreError(f"could not parse blob URL from CLI output: {out!r}")
        self._urls[key] = m.group(0)
        return key

    def put_bytes(self, key, data, content_type=None):
        import tempfile
        suffix = Path(key).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            return self.put_file(key, tmp, content_type)
        finally:
            os.unlink(tmp)

    def url_for(self, key):
        if key in self._urls:
            return self._urls[key]
        if self.base_url:
            return f"{self.base_url}/{key}"
        raise BlobStoreError(
            f"no URL known for {key!r}: set BLOB_BASE_URL, or upload through "
            "this process so the CLI-reported URL is captured")

    def exists(self, key):
        try:
            self._run(["get", key, "--access", "public", "--output", os.devnull])
            return True
        except BlobStoreError:
            return False


_STORE = None


def get_store():
    """Backend selected by BLOB_BACKEND (`local` default, or `vercel`)."""
    global _STORE
    if _STORE is None:
        _STORE = make_store(os.environ.get("BLOB_BACKEND", "local"))
    return _STORE


def make_store(backend):
    if backend == "local":
        return LocalBlobStore()
    if backend == "vercel":
        return VercelBlobStore()
    raise BlobStoreError(f"unknown BLOB_BACKEND: {backend!r}")


def set_store(store):
    """Override the process-wide store (tests, and the batch publisher)."""
    global _STORE
    _STORE = store
    return store


def reset_store():
    global _STORE
    _STORE = None
