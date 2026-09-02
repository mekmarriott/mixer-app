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


def master_key(track_id, ext=None):
    """The master object the BROWSER downloads — compressed (see
    config.AUDIO_DELIVERY_CODEC). This is what `tracks.audio_key` holds."""
    return f"audio/{track_id}.{ext or config.delivery_ext()}"


def master_source_key(track_id):
    """The canonical 16-bit PCM master, which stays local and is never served.

    Kept separate from `master_key` because the two have different jobs. Every
    variant is time-stretched FROM this file, so it has to be lossless or each
    render would compound the previous encoder's artefacts — whereas the copy
    that crosses the network wants to be as small as it can be. They are also
    written at different times: this one the moment audio is fetched, the
    delivery encoding when the track is rendered.
    """
    return f"audio/{track_id}.wav"


def variant_key(track_id, grid_bpm, ext=None):
    """A rendered variant, in the delivery encoding.

    Unlike a master there is no lossless counterpart: a variant is an output,
    re-derived from the master whenever the grid changes, so nothing ever reads
    it back to render from.
    """
    return f"variants/{track_id}_{int(grid_bpm)}.{ext or config.delivery_ext()}"


def put_delivery(store, key, samples, sr):
    """Write `samples` to `key` in the delivery encoding. Returns the key.

    The local backend hands back a real path so the encode lands straight in
    its final location; a remote backend stages to a temp file and uploads.
    Shared by the master and variant paths so the two cannot drift on the
    encoding, the MIME type, or the staging dance.
    """
    from .audio_io import encode_delivery      # local: audio_io pulls in numpy

    dst = store.local_path(key)
    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        encode_delivery(samples, sr, dst)
        return key
    import tempfile
    ext, mime, _ = config.delivery_format()
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        tmp = f.name
    try:
        encode_delivery(samples, sr, tmp)
        return store.put_file(key, tmp, mime)
    finally:
        os.unlink(tmp)


def meta_key(track_id):
    """Sidecar holding the source metadata a master was ingested with.

    Written next to every master so that rebuilding the catalog costs zero
    API requests: without it, a wiped database forces a re-fetch of metadata
    for tracks whose audio is already on disk, which spends monthly Jamendo
    quota to re-learn something we already knew. See publish.fetch_masters.
    """
    return f"meta/{track_id}.json"


def _content_type(key):
    """The MIME type to store `key` under.

    The configured delivery formats win over `mimetypes`, which is wrong for
    the container this project actually ships: it maps `.m4a` to
    `audio/mp4a-latm` — a raw AAC-LATM elementary stream, not an MP4 container
    — and browsers refuse it. It also spells WAV `audio/x-wav` where
    DELIVERY_FORMATS says `audio/wav`, so an object's type depended on which
    code path uploaded it. `put_delivery` passes the configured type explicitly
    on its remote path; anything reaching `put_file` without one (a re-upload
    from `backend.reconcile`, say) came through here instead and has to agree.
    """
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    for fmt_ext, mime, _encoder in config.DELIVERY_FORMATS.values():
        if ext == fmt_ext:
            return mime
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

    def list_blobs(self, prefix=""):
        """`{key: size_in_bytes}` for the store, optionally under a prefix.

        Answers the question `exists()` cannot — "what is in the store that
        the catalog never named?" — and carries sizes, because an audit that
        knows only which keys are present cannot tell a current object from a
        stale one sitting at the same key.
        """
        raise NotImplementedError

    def list_keys(self, prefix=""):
        return sorted(self.list_blobs(prefix))

    def delete(self, key):
        """Remove an object. Idempotent: deleting an absent key is not an error."""
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

    def list_blobs(self, prefix=""):
        root = self.root
        if not root.is_dir():
            return {}
        found = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix):
                found[key] = path.stat().st_size
        return found

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)


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

    #: A store is created public OR private and will not accept writes at the
    #: other access level — a private store rejects `--access public` outright.
    #:
    #: This is not merely a flag. The whole serving design is a 302 from the API
    #: straight to the object, which requires the object to be anonymously
    #: readable. Against a PRIVATE store that redirect 404s in the browser, and
    #: private delivery is proxied through a Function rather than served from
    #: the CDN — roughly double-billed, and it puts the audio back on the path
    #: the redirect exists to keep it off.
    #:
    #: So `public` is the supported configuration. `private` is accepted here
    #: only so uploads succeed against a private store; serving from one needs
    #: signed URLs, which url_for() does not implement.
    DEFAULT_ACCESS = "public"

    #: A store's public delivery host is its id without the `store_` prefix,
    #: lowercased. Vercel's Blob integration injects BLOB_STORE_ID but not the
    #: URL, so deriving it means a deployment does not need BLOB_BASE_URL set
    #: by hand — and cannot end up pointing at a different store than the token
    #: writes to, which is exactly the failure that produces objects that
    #: upload fine and 404 on every read.
    STORE_HOST_SUFFIX = ".public.blob.vercel-storage.com"

    @classmethod
    def base_url_from_store_id(cls, store_id):
        store_id = (store_id or "").strip()
        if not store_id.startswith("store_"):
            return ""
        return f"https://{store_id.removeprefix('store_').lower()}{cls.STORE_HOST_SUFFIX}"

    def __init__(self, token=None, base_url=None, cli="vercel", access=None):
        self.token = token or os.environ.get("BLOB_READ_WRITE_TOKEN")
        self.base_url = (
            base_url
            or os.environ.get("BLOB_BASE_URL")
            or self.base_url_from_store_id(os.environ.get("BLOB_STORE_ID"))
            or ""
        ).rstrip("/")
        self.access = access or os.environ.get("BLOB_ACCESS") or self.DEFAULT_ACCESS
        if self.access not in ("public", "private"):
            raise BlobStoreError(
                f"BLOB_ACCESS must be 'public' or 'private', got {self.access!r}")
        self.cli = cli
        self._urls = {}

    @property
    def argv(self):
        """The CLI as a command list. A string is one word; a sequence is used
        as given, so `["npx", "--yes", "vercel@latest"]` works without a global
        install."""
        return [self.cli] if isinstance(self.cli, str) else list(self.cli)

    def _env(self):
        """The subprocess environment, minus a half-configured OIDC setup.

        The CLI refuses to run when BLOB_STORE_ID is set without
        VERCEL_OIDC_TOKEN ("must both be set, or both be unset"). The Vercel
        Supabase/Blob integration writes BLOB_STORE_ID into .env on its own,
        and config.load_env_file() then puts it in os.environ — so simply
        having pulled the environment breaks every upload with an error about
        OIDC that names nothing the caller set. Drop it when authenticating
        with a read-write token, which is the only mode this class uses.
        """
        env = dict(os.environ)
        if self.token and not env.get("VERCEL_OIDC_TOKEN"):
            env.pop("BLOB_STORE_ID", None)
        return env

    def _run(self, args):
        argv = self.argv
        if not shutil.which(argv[0]):
            raise BlobStoreError(
                f"{argv[0]!r} not on PATH — the Vercel CLI is required to "
                "upload to Vercel Blob (npm i -g vercel)")
        proc = subprocess.run([*argv, "blob", *args],
                              capture_output=True, text=True, env=self._env())
        if proc.returncode != 0:
            raise BlobStoreError(
                f"vercel blob {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}")
        # Both streams. The CLI writes its progress *and* its "Success! <url>"
        # line to stderr, leaving stdout empty, so scanning stdout alone finds
        # no URL and makes every successful upload look like a parse failure.
        return f"{proc.stdout}\n{proc.stderr}"

    def put_file(self, key, src_path, content_type=None):
        args = [
            "put", str(src_path),
            "--pathname", key,
            "--access", self.access,
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
        if m:
            self._urls[key] = m.group(0)
        elif self.access == "public":
            # Only a public store owes us a servable URL; without it the
            # serving path has nothing to redirect to. A private store has no
            # such URL to report, and demanding one would fail an upload that
            # actually succeeded.
            raise BlobStoreError(f"could not parse blob URL from CLI output: {out!r}")
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
        if self.access != "public":
            raise BlobStoreError(
                "cannot hand a client a URL for a private blob: the audio "
                "endpoint 302s to it, and a private object is not anonymously "
                "readable, so the browser gets a 404. Serving from a private "
                "store needs signed URLs, which are not implemented. Set the "
                "store to public access, or add signing here.")
        if key in self._urls:
            return self._urls[key]
        if self.base_url:
            return f"{self.base_url}/{key}"
        raise BlobStoreError(
            f"no URL known for {key!r}: set BLOB_BASE_URL, or upload through "
            "this process so the CLI-reported URL is captured")

    def exists(self, key):
        try:
            self._run(["get", key, "--access", self.access, "--output", os.devnull])
            return True
        except BlobStoreError:
            return False

    #: A row of `vercel blob list`: uploadedAt, size, pathname, url. The CLI
    #: has no machine-readable output mode, so the URL — the one column with
    #: an unambiguous shape — anchors the match and the size is read back from
    #: the columns before it.
    LIST_ROW_RE = re.compile(
        r"^\s*\S+\s+(?P<size>\d+)\s+(?P<pathname>\S+)\s+"
        r"(?P<url>https://\S+\.blob\.vercel-storage\.com/\S+)\s*$")
    #: `> To display the next page run \`vercel blob list ... --next <cursor>\``
    LIST_CURSOR_RE = re.compile(r"--next\s+(\S+?)`")

    def list_blobs(self, prefix=""):
        """`{key: size}` for the whole store, following the CLI's pagination."""
        found, cursor, seen_cursors = {}, None, set()
        while True:
            args = ["list", "--limit", "1000"]
            if prefix:
                args += ["--prefix", prefix]
            if cursor:
                args += ["--next", cursor]
            if self.token:
                args += ["--rw-token", self.token]
            out = self._run(args)
            for line in out.splitlines():
                m = self.LIST_ROW_RE.match(line)
                if m:
                    found[m.group("pathname")] = int(m.group("size"))
            m = self.LIST_CURSOR_RE.search(out)
            # A cursor that repeats means the page did not advance; stop rather
            # than page forever.
            if not m or m.group(1) in seen_cursors:
                return found
            cursor = m.group(1)
            seen_cursors.add(cursor)

    def delete(self, key):
        args = ["del", key]
        if self.token:
            args += ["--rw-token", self.token]
        self._run(args)


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
