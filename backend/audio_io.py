"""WAV read/write helpers (stdlib `wave` + numpy; mono, 16-bit PCM)."""
import wave

import numpy as np


def save_wav(path, samples, sr):
    samples = np.asarray(samples, dtype=np.float64)
    peak = np.max(np.abs(samples)) or 1.0
    if peak > 1.0:
        samples = samples / peak
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr
