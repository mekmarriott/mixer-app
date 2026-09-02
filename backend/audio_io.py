"""WAV read/write helpers (stdlib `wave` + numpy; mono, 16-bit PCM)."""
import wave

import numpy as np

# Signed 16-bit PCM spans -32768..32767. Decoding divides by 32768 so a
# full-scale negative sample maps to exactly -1.0 rather than -1.00003 —
# real (as opposed to synthesised) audio does hit that rail. Encoding scales
# by 32767 so +1.0 cannot overflow the positive rail.
PCM16_DECODE_SCALE = 32768.0
PCM16_ENCODE_SCALE = 32767.0


def pcm16_to_float(raw):
    """Little-endian signed 16-bit PCM bytes -> float64 in [-1.0, 1.0]."""
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / PCM16_DECODE_SCALE


def save_wav(path, samples, sr):
    samples = np.asarray(samples, dtype=np.float64)
    peak = np.max(np.abs(samples)) or 1.0
    if peak > 1.0:
        samples = samples / peak
    pcm = (samples * PCM16_ENCODE_SCALE).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def encode_delivery(samples, sr, dst, codec=None, bitrate=None):
    """Write `samples` to `dst` in the browser-delivery encoding.

    The rendered PCM is what the pipeline keeps; this is the copy the client
    downloads. Encoding runs at ingest time on a developer machine — the same
    place ffmpeg already decodes Jamendo's MP3s — so the serving function never
    needs a codec, and nothing about this reaches Vercel.

    Returns the MIME type to store the object with.

    The PCM is handed to ffmpeg on stdin as a real WAV (header included) rather
    than as a raw stream, so the sample rate and layout travel with the data
    instead of having to be restated as flags that could disagree with it.
    """
    import io
    import subprocess

    from . import config

    ext, mime, encoder = (config.delivery_format() if codec is None else
                          config.DELIVERY_FORMATS[codec])
    if encoder is None:                       # "wav": no encoding to do
        save_wav(dst, samples, sr)
        return mime

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        samples = np.asarray(samples, dtype=np.float64)
        peak = np.max(np.abs(samples)) or 1.0
        if peak > 1.0:
            samples = samples / peak
        w.writeframes((samples * PCM16_ENCODE_SCALE).astype("<i2").tobytes())

    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-c:a", encoder, "-b:a", bitrate or config.AUDIO_DELIVERY_BITRATE,
         "-ac", "1", str(dst)],
        input=buf.getvalue(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed to encode %s: %s"
            % (ext, proc.stderr.decode(errors="replace").strip()[:400]))
    return mime


def wav_duration(path):
    """Seconds of audio in a WAV file, read from the header alone.

    Lets resumed ingestion re-register an already-rendered variant without
    decoding it or re-running the stretch.
    """
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = pcm16_to_float(raw)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr
