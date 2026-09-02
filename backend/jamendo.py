"""Track source provider (Phase 1 step 1).

Two modes, selected by config/tracks.json:
  jamendo  -- real Jamendo API v3.0 (requires network + a client id).
              Only tracks with audiodownload_allowed=true are accepted
              (test P1-01), and the per-track CC license is read from the
              API's license_ccurl field. Metadata lookups retry the empty
              HTTP 200 the API intermittently returns — see
              EMPTY_RESULT_RETRIES and fetch_track_metadata below.
  offline  -- deterministic synthesis from the same config entries, so the
              full pipeline runs without network. License/BPM/key come from
              the config entry, standing in for API metadata.

Both return the same shape: (metadata dict, samples ndarray, sample_rate).
"""
import re
import time

import numpy as np

from . import config, ratelimit, storage, licensing, synth
from .audio_io import pcm16_to_float, save_wav

JAMENDO_API = "https://api.jamendo.com/v3.0"

# Jamendo intermittently answers a perfectly valid track query with HTTP 200,
# an envelope reporting success (code 0), and an *empty* results array. It was
# measured on the live API at a 27-50% rate, and the loss is all-or-nothing
# rather than partial. So an empty success is never evidence that a track is
# gone — it means "ask again". Treating it as absence silently drops tracks
# from an import that then reports success, which is the failure mode this
# retry exists to prevent.
EMPTY_RESULT_RETRIES = 4
RETRY_BASE_DELAY_S = 0.5

# The audio host (prod-1.storage.jamendo.com) is separate infrastructure from
# the API and fails differently — connection resets and truncated bodies rather
# than empty envelopes — so it gets its own retry budget.
DOWNLOAD_RETRIES = 4
DOWNLOAD_TIMEOUT_S = 180


class TrackSourceError(Exception):
    pass


class IncompatibleLicense(TrackSourceError):
    """The track's licence forbids what this app would do with the audio.

    Raised from the `accept` hook BEFORE the download, so an unusable track
    costs one cheap metadata request rather than a multi-megabyte transfer,
    an analysis pass and a full set of rendered variants.
    """


def validate_source_meta(meta):
    """Hard gate (P1-01): only tracks whose source permits raw-audio download
    may enter the pipeline. Applied to every provider's metadata."""
    if not meta.get("audiodownload_allowed", False):
        raise TrackSourceError(
            f"Track {meta.get('id')}: audiodownload_allowed is false — rejected")
    return meta


def fetch_track(entry, mode, accept=None):
    """Metadata + decoded audio for one track.

    `accept(meta)` is called once the metadata is known and BEFORE the audio is
    downloaded. It may raise (see IncompatibleLicense) to abandon the track
    while it is still free to do so. Licence and download permission both come
    from that metadata, so every gate belongs at this point — after one small
    request, before the expensive one.
    """
    if mode == "offline":
        return _fetch_offline(entry, accept)
    if mode == "jamendo":
        return _fetch_jamendo(entry, accept)
    raise TrackSourceError(f"Unknown source mode: {mode}")


def _fetch_offline(entry, accept=None):
    meta = {
        "id": str(entry["id"]),
        "name": entry["name"],
        "artist": entry["artist"],
        "genre": entry["genre"],
        "license": entry["license"],
        "audiodownload_allowed": True,
    }
    validate_source_meta(meta)
    if accept is not None:
        # Same gate as the network path, so tests exercise the real ordering:
        # nothing is synthesised for a track that will be rejected.
        accept(meta)
    samples = synth.synthesize(entry)
    return meta, samples, config.SAMPLE_RATE


def _http_get_json(url, params, timeout=30):  # pragma: no cover - requires network
    import requests

    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_track_metadata(track_id, client_id, get=_http_get_json, sleep=time.sleep):
    """One track's API metadata, retrying Jamendo's empty-but-successful replies.

    Distinguishes three outcomes that the previous one-shot call collapsed into
    "not found":

      * envelope reports an error code -> fail immediately, no retry (a bad
        client id or a malformed query does not get better by asking again);
      * HTTP 200, code 0, empty results  -> transient, retry with backoff;
      * results present                  -> return the first.

    Exhausting the retries still raises, but says what actually happened rather
    than asserting the track is missing.
    """
    params = {"client_id": client_id, "id": track_id, "format": "json",
              "audioformat": "mp32", "include": "licenses musicinfo"}
    for attempt in range(EMPTY_RESULT_RETRIES + 1):
        payload = get(f"{JAMENDO_API}/tracks", params)
        headers = payload.get("headers") or {}
        code = headers.get("code", 0)
        if code:
            raise TrackSourceError(
                "Jamendo API error %s for track %s: %s"
                % (code, track_id, headers.get("error_message") or "no message"))
        results = payload.get("results") or []
        if results:
            return results[0]
        if attempt < EMPTY_RESULT_RETRIES:
            sleep(RETRY_BASE_DELAY_S * (2 ** attempt))
    raise TrackSourceError(
        "Jamendo returned an empty success for track %s on all %d attempts. "
        "This is not proof the track is gone: Jamendo answers valid queries "
        "with an empty HTTP 200 intermittently. Re-run before treating the "
        "track as missing." % (track_id, EMPTY_RESULT_RETRIES + 1))


def require_client_id():
    """The configured Jamendo client id, or a message saying how to set one."""
    client_id = config.jamendo_client_id()
    if not client_id:
        raise TrackSourceError(
            "No Jamendo client id: set JAMENDO_API_CLIENT (or JAMENDO_CLIENT_ID) "
            "in the environment or in .env")
    return client_id


def download_audio(url, retries=DOWNLOAD_RETRIES, sleep=time.sleep, limiter=None):
    """Fetch a track's audio bytes, retrying transient transport failures.

    Separate from the metadata retry above: this talks to the storage host, so
    the failures worth retrying are connection errors, 5xx/429, and empty
    bodies rather than Jamendo's empty-success envelope.

    `limiter` paces requests when a batch run is downloading many tracks at
    once. It is optional because the interactive path fetches one track and has
    nothing to pace against."""
    import requests

    last = "no attempts made"
    for attempt in range(retries):
        if attempt:
            sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
        if limiter is not None:
            limiter.acquire()
        try:
            r = requests.get(url, timeout=DOWNLOAD_TIMEOUT_S)
        except requests.RequestException as exc:
            last = f"request failed: {exc}"
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last = f"HTTP {r.status_code}"
            continue
        r.raise_for_status()
        if not r.content:
            last = "empty body"
            continue
        return r.content
    raise TrackSourceError(
        f"Jamendo audio download failed after {retries} attempts: {last}")


def decode_to_samples(encoded, sr=None):
    """Decode compressed audio bytes to mono float64 at our sample rate.

    ffmpeg handles Jamendo's MP3s; the intermediate is 16-bit PCM WAV so the
    stdlib `wave` reader can pick it up without another dependency."""
    import subprocess
    import tempfile
    import wave as wave_mod

    sr = sr or config.SAMPLE_RATE
    with tempfile.TemporaryDirectory() as td:
        src, dst = f"{td}/in.audio", f"{td}/out.wav"
        with open(src, "wb") as f:
            f.write(encoded)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-ac", "1", "-ar", str(sr), "-sample_fmt", "s16", dst],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise TrackSourceError(
                f"ffmpeg failed to decode Jamendo audio: {proc.stderr.strip()[:400]}")
        with wave_mod.open(dst, "rb") as w:
            raw = w.readframes(w.getnframes())
    return pcm16_to_float(raw)


def _fetch_jamendo(entry, accept=None):  # pragma: no cover - requires network
    t = fetch_track_metadata(entry["id"], require_client_id())
    meta = {"id": str(t["id"]), "name": t["name"], "artist": t["artist_name"],
            "genre": entry.get("genre", "house"),
            "license": _license_from_ccurl(t.get("license_ccurl", "")),
            "audiodownload_allowed": bool(t.get("audiodownload_allowed", False)),
            "source_url": t.get("shareurl", "")}
    validate_source_meta(meta)                       # P1-01, before any download
    if accept is not None:
        accept(meta)                                 # licence gate, same reason

    url = t.get("audiodownload")
    if not url:
        raise TrackSourceError(f"Jamendo track {t['id']} exposes no download URL")
    samples = decode_to_samples(download_audio(url))
    return meta, samples, config.SAMPLE_RATE


# creativecommons.org/licenses/<slug>/<version>[/<jurisdiction>]/
_CCURL_RE = re.compile(
    r"creativecommons\.org/licenses/"
    r"(?P<slug>by(?:-nc)?(?:-sa|-nd)?)/"
    r"(?P<version>" + "|".join(v.replace(".", r"\.")
                               for v in licensing.CC_VERSIONS) + r")/"
    r"(?:(?P<jurisdiction>[a-z]{2})/)?", re.I)


def _license_from_ccurl(url):
    """Canonical license name from Jamendo's license_ccurl, version included.

    Jamendo's catalog is mostly CC 3.0 with some ported 2.x; collapsing those
    to "4.0" would record a licence the track was never released under."""
    m = _CCURL_RE.search(url or "")
    if not m:
        raise TrackSourceError(f"Unrecognized CC url: {url!r}")
    name = f"CC {m.group('slug').upper()} {m.group('version')}"
    if m.group("jurisdiction"):
        name += " " + m.group("jurisdiction").upper()
    return name


def persist_master(meta, samples, sr, store=None):
    """Write the master through the blob store and return its object key.

    A JSON sidecar of the source metadata goes alongside it. That is what lets
    a later run reuse this master without touching the API at all: without it,
    rebuilding a wiped catalog would re-spend monthly quota re-learning
    metadata whose audio is already on disk (see publish.fetch_masters).
    """
    import json

    store = store or storage.get_store()
    # The PCM master, not the delivery encoding: this is the file every variant
    # is rendered from, and the render stage reads it back by this key.
    key = storage.master_source_key(meta["id"])
    store.put_bytes(storage.meta_key(meta["id"]),
                    json.dumps(meta, indent=2).encode(), "application/json")
    dst = store.local_path(key)
    if dst is not None:                       # local backend: write in place
        dst.parent.mkdir(parents=True, exist_ok=True)
        save_wav(dst, samples, sr)
        return key
    import tempfile                           # remote backend: stage then upload
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        save_wav(tmp, samples, sr)
        return store.put_file(key, tmp, "audio/wav")
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------
# Batched metadata — the batch-publisher path
# --------------------------------------------------------------------------
#
# `fetch_track_metadata` above answers one id per request, which is right for
# the interactive flow and ruinous for a catalog: 10,000 tracks would be 10,000
# requests against a monthly quota. Everything below asks for up to 200 ids at
# a time while preserving the same rules — retry an empty success, never retry
# a real API error.

#: Documented maximum for the `limit` parameter on /tracks — how many results
#: one response may carry.
MAX_RESULTS_PER_REQUEST = 200

#: How many ids one batched lookup may ask for, which is a DIFFERENT and much
#: smaller limit: the API caps any multi-value parameter at 50 and rejects the
#: whole request past it ("The maximum number of multiple values a parameter
#: can receive is: 50"). Batching by the `limit` cap instead looks right and
#: works for any catalog under 50 tracks, then fails outright on the first
#: import bigger than that — which is exactly when a batched fetch is the only
#: affordable option.
MAX_IDS_PER_REQUEST = 50

#: Deliberately conservative. 2.0 req/s is the highest rate observed to run
#: clean against the live API (30 serial calls, zero 429s) — a floor on the
#: real ceiling, not the ceiling. The API publishes no rate-limit headers of
#: any kind, so there is no telemetry to discover the true limit from.
DEFAULT_API_RATE = 2.0          # requests/sec against api.jamendo.com
DEFAULT_DOWNLOAD_RATE = 2.0     # file fetches/sec against the storage host


class RateLimited(TrackSourceError):
    """The server signalled 429 and the retries were exhausted."""


class IncompleteBatch(TrackSourceError):
    """A batch came back short after exhausting the empty-success retries."""


def estimate_api_requests(n_tracks):
    """Requests needed to fetch metadata for `n_tracks`. Used by publish.py to
    report cost against the monthly quota before spending any of it."""
    return -(-int(n_tracks) // MAX_IDS_PER_REQUEST)     # ceil division


def _batch_get_json(url, params, timeout=30, limiter=None, budget=None):
    """A paced, budgeted GET returning parsed JSON.

    The budget is spent *before* the request, so an exhausted budget raises
    without touching the network. A monthly quota is consumed by retry loops,
    not by bursts, which is why the ceiling is checked per attempt.
    """
    import requests

    if budget is not None:
        budget.spend(1)
    if limiter is not None:
        limiter.acquire()
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited(f"429 from {url}")
    r.raise_for_status()
    return r.json()


def fetch_metadata(track_ids, genre_by_id=None, limiter=None, budget=None,
                   client_id=None, get=None, sleep=time.sleep):
    """Metadata for many track ids in as few API requests as possible."""
    ids = [str(t) for t in track_ids]
    if not ids:
        return {}
    client_id = client_id or require_client_id()
    genre_by_id = genre_by_id or {}
    limiter = limiter or ratelimit.TokenBucket(DEFAULT_API_RATE)
    get = get or _batch_get_json

    out = {}
    for start in range(0, len(ids), MAX_IDS_PER_REQUEST):
        chunk = ids[start:start + MAX_IDS_PER_REQUEST]
        for t in _fetch_batch(chunk, client_id, get, limiter, budget, sleep):
            tid = str(t["id"])
            out[tid] = _meta_from_api(t, genre_by_id.get(tid, "house"))
    return out


def _fetch_batch(chunk, client_id, get, limiter, budget, sleep,
                 attempts=EMPTY_RESULT_RETRIES + 1):
    """One id-batch, retried until the API returns a complete result set.

    Same three-way outcome as `fetch_track_metadata`, applied to a batch: an
    error envelope fails immediately, a short result set is transient and
    retried, a complete set is returned. The distinction matters more here —
    retrying a bad client id 5 times per batch across 50 batches would spend
    250 requests of a monthly quota to learn nothing.

    A short batch is never evidence that tracks were delisted. The loss was
    measured as all-or-nothing (a 12-id batch returned 12/12 or 0/12 across ten
    runs, never 9), so accepting one would silently drop tracks from an import
    that then reports success.
    """
    got = []
    for attempt in range(attempts):
        payload = get(f"{JAMENDO_API}/tracks", {
            # Multiple ids must reach the wire as `id=a+b+c`. Repeating the
            # `id=` parameter does NOT batch — the API keeps the last one and
            # returns a single result. requests urlencodes a space to `+`, so
            # joining on a space produces the required form; joining on a
            # literal "+" would be escaped to %2B and silently break batching.
            "client_id": client_id,
            "id": " ".join(chunk),
            "format": "json",
            "audioformat": "mp32",
            "limit": MAX_RESULTS_PER_REQUEST,
            "include": "licenses musicinfo stats",
        }, limiter=limiter, budget=budget)
        headers = payload.get("headers") or {}
        code = headers.get("code", 0)
        if code:
            raise TrackSourceError(
                "Jamendo API error %s for a batch of %d track(s): %s"
                % (code, len(chunk), headers.get("error_message") or "no message"))
        got = payload.get("results") or []
        if len(got) >= len(chunk):
            return got
        if attempt < attempts - 1:
            sleep(RETRY_BASE_DELAY_S * (2 ** attempt))
    raise IncompleteBatch(
        "Jamendo returned %d/%d tracks on all %d attempts. This is not proof "
        "the missing ones are gone: the API answers valid queries with an "
        "empty HTTP 200 intermittently. Re-run before treating them as "
        "missing." % (len(got), len(chunk), attempts))


def _meta_from_api(t, genre):
    return {
        "id": str(t["id"]),
        "name": t["name"],
        "artist": t["artist_name"],
        "genre": genre,
        "license": _license_from_ccurl(t.get("license_ccurl", "")),
        "audiodownload_allowed": bool(t.get("audiodownload_allowed", False)),
        "audiodownload": t.get("audiodownload", ""),
        # Total listens, all time. `include=stats` already asked for this and
        # then dropped it on the floor, which left the zero state with no
        # popularity to order by and nothing to show on a row.
        "popularity": _listens(t),
    }


def _listens(t):
    stats = t.get("stats") or {}
    value = stats.get("rate_listened_total")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
