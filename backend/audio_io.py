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
