"""Track source provider (Phase 1 step 1).

Two modes, selected by config/tracks.json:
  jamendo  -- real Jamendo API v3.0 (requires network + JAMENDO_CLIENT_ID).
              Only tracks with audiodownload_allowed=true are accepted
              (test P1-01), and the per-track CC license is read from the
              API's license_ccurl field.
  offline  -- deterministic synthesis from the same config entries, so the
              full pipeline runs without network. License/BPM/key come from
              the config entry, standing in for API metadata.

Both return the same shape: (metadata dict, samples ndarray, sample_rate).
"""
import os

import numpy as np

from . import config, licensing, synth
from .audio_io import save_wav

JAMENDO_API = "https://api.jamendo.com/v3.0"


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


def _fetch_jamendo(entry):  # pragma: no cover - requires network
    import io
    import wave as wave_mod

    import requests

    client_id = os.environ.get("JAMENDO_CLIENT_ID")
    if not client_id:
        raise TrackSourceError("JAMENDO_CLIENT_ID env var required for jamendo mode")
    r = requests.get(f"{JAMENDO_API}/tracks", params={
        "client_id": client_id, "id": entry["id"], "format": "json",
        "audioformat": "mp32", "include": "licenses"}, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise TrackSourceError(f"Jamendo track {entry['id']} not found")
    t = results[0]
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
