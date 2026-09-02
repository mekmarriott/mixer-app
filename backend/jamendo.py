"""Track source provider (Phase 1 step 1).

Two modes, selected by config/tracks.json:
  jamendo  -- real Jamendo API v3.0 (requires network + JAMENDO_CLIENT_ID).
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
import os
import time

import numpy as np

from . import config, licensing, synth
from .audio_io import save_wav

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
              "audioformat": "mp32", "include": "licenses"}
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


def _fetch_jamendo(entry):  # pragma: no cover - requires network
    import wave as wave_mod

    import requests

    client_id = os.environ.get("JAMENDO_CLIENT_ID")
    if not client_id:
        raise TrackSourceError("JAMENDO_CLIENT_ID env var required for jamendo mode")
    t = fetch_track_metadata(entry["id"], client_id)
    license_name = _license_from_ccurl(t.get("license_ccurl", ""))
    meta = {"id": str(t["id"]), "name": t["name"], "artist": t["artist_name"],
            "genre": entry.get("genre", "house"), "license": license_name,
            "audiodownload_allowed": bool(t.get("audiodownload_allowed", False))}
    validate_source_meta(meta)
    audio = requests.get(t["audiodownload"], timeout=120)
    audio.raise_for_status()
    # Decode via ffmpeg to mono WAV at our sample rate.
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = f"{td}/in.mp3"
        dst = f"{td}/out.wav"
        with open(src, "wb") as f:
            f.write(audio.content)
        subprocess.run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar",
                        str(config.SAMPLE_RATE), dst], check=True, capture_output=True)
        with wave_mod.open(dst, "rb") as w:
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767.0
    return meta, samples, config.SAMPLE_RATE


def _license_from_ccurl(url):
    mapping = {
        "by-nc-nd": "CC BY-NC-ND 4.0", "by-nc-sa": "CC BY-NC-SA 4.0",
        "by-nc": "CC BY-NC 4.0", "by-nd": "CC BY-ND 4.0",
        "by-sa": "CC BY-SA 4.0", "by": "CC BY 4.0",
    }
    for k, v in mapping.items():   # longest keys first by construction above
        if f"/{k}/" in url:
            return v
    raise TrackSourceError(f"Unrecognized CC url: {url}")


def persist_master(meta, samples, sr):
    path = config.AUDIO_DIR / f"{meta['id']}.wav"
    save_wav(path, samples, sr)
    return path
