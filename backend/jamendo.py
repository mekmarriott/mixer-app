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

from . import config, licensing, synth
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


def validate_source_meta(meta):
    """Hard gate (P1-01): only tracks whose source permits raw-audio download
    may enter the pipeline. Applied to every provider's metadata."""
    if not meta.get("audiodownload_allowed", False):
        raise TrackSourceError(
            f"Track {meta.get('id')}: audiodownload_allowed is false — rejected")
    return meta


def fetch_track(entry, mode):
    if mode == "offline":
        return _fetch_offline(entry)
    if mode == "jamendo":
        return _fetch_jamendo(entry)
    raise TrackSourceError(f"Unknown source mode: {mode}")


def _fetch_offline(entry):
    meta = {
        "id": str(entry["id"]),
        "name": entry["name"],
        "artist": entry["artist"],
        "genre": entry["genre"],
        "license": entry["license"],
        "audiodownload_allowed": True,
    }
    validate_source_meta(meta)
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
            "No Jamendo client id: set JAMENDO_CLIENT_ID (or JAMENDO_API_CLIENT) "
            "in the environment or in .env")
    return client_id


def download_audio(url, retries=DOWNLOAD_RETRIES, sleep=time.sleep):
    """Fetch a track's audio bytes, retrying transient transport failures.

    Separate from the metadata retry above: this talks to the storage host, so
    the failures worth retrying are connection errors, 5xx/429, and empty
    bodies rather than Jamendo's empty-success envelope."""
    import requests

    last = "no attempts made"
    for attempt in range(retries):
        if attempt:
            sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
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


def _fetch_jamendo(entry):  # pragma: no cover - requires network
    t = fetch_track_metadata(entry["id"], require_client_id())
    meta = {"id": str(t["id"]), "name": t["name"], "artist": t["artist_name"],
            "genre": entry.get("genre", "house"),
            "license": _license_from_ccurl(t.get("license_ccurl", "")),
            "audiodownload_allowed": bool(t.get("audiodownload_allowed", False)),
            "source_url": t.get("shareurl", "")}
    validate_source_meta(meta)                       # P1-01, before any download

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


def persist_master(meta, samples, sr):
    path = config.AUDIO_DIR / f"{meta['id']}.wav"
    save_wav(path, samples, sr)
    return path
