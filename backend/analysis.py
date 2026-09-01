"""Audio analysis (Phase 1).

Provider seam: if `essentia` is importable, `analyze()` can be routed to it in
production. In this environment the fallback implements the same contract with
numpy/scipy:

  BPM           onset-strength autocorrelation (spectral-flux novelty)
  beat grid     comb-phase search at the detected period
  key           chroma (STFT -> pitch-class fold) x Krumhansl-Schmuckler
  frames        RMS energy, spectral flux, bass-band energy per hop
  prefix sums   cumulative sums over frame features for O(1) window queries

Output is a plain dict (JSON-serializable) cached by the ingestion pipeline.
"""
import numpy as np
from scipy import signal as sp_signal

from . import config

try:  # pragma: no cover - not available in this environment
    import essentia.standard  # noqa: F401
    HAVE_ESSENTIA = True
except Exception:
    HAVE_ESSENTIA = False

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler key profiles
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# pitch class + mode -> Camelot
CAMELOT = {
    ("A", "minor"): "8A", ("E", "minor"): "9A", ("B", "minor"): "10A",
    ("F#", "minor"): "11A", ("C#", "minor"): "12A", ("G#", "minor"): "1A",
    ("D#", "minor"): "2A", ("A#", "minor"): "3A", ("F", "minor"): "4A",
    ("C", "minor"): "5A", ("G", "minor"): "6A", ("D", "minor"): "7A",
    ("C", "major"): "8B", ("G", "major"): "9B", ("D", "major"): "10B",
    ("A", "major"): "11B", ("E", "major"): "12B", ("B", "major"): "1B",
    ("F#", "major"): "2B", ("C#", "major"): "3B", ("G#", "major"): "4B",
    ("D#", "major"): "5B", ("A#", "major"): "6B", ("F", "major"): "7B",
}


def _stft_mag(samples, sr):
    f, t, z = sp_signal.stft(samples, fs=sr, nperseg=config.FRAME_SIZE,
                             noverlap=config.FRAME_SIZE - config.HOP_SIZE, padded=False)
    return f, t, np.abs(z)


def _onset_strength(mag):
    """Spectral flux novelty: positive magnitude increases summed over bins."""
    diff = np.diff(mag, axis=1)
    flux = np.sum(np.maximum(diff, 0.0), axis=0)
    return np.concatenate([[0.0], flux])


def detect_bpm(onset, hop_dur, bpm_lo=70, bpm_hi=180):
    """Autocorrelation tempo estimate with octave disambiguation.

    The raw AC peak often sits on the half-beat pulse (kick + offbeat hats).
    After the argmax, repeatedly double the lag while the doubled lag remains
    nearly as strong and in range — preferring the fundamental beat period
    over its subdivisions."""
    x = onset - onset.mean()
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    if ac[0] > 0:
        ac = ac / ac[0]
    lag_lo = max(1, int(round(60.0 / bpm_hi / hop_dur)))
    lag_hi = min(len(ac) - 1, int(round(60.0 / bpm_lo / hop_dur)))
    if lag_hi <= lag_lo:
        return 120.0
    lag0 = lag_lo + int(np.argmax(ac[lag_lo:lag_hi + 1]))

    # The raw peak can sit on a subdivision (1/2 beat) or a polyrhythmic
    # relative (2/3 beat). Compare comb scores across candidate multiples of
    # the found lag and keep the best-supported period in range.
    def comb(lag_f):
        s, k = 0.0, 1
        while k <= 4 and int(round(lag_f * k)) < len(ac):
            s += ac[int(round(lag_f * k))] / k
            k += 1
        return s

    best_lag, best_score = float(lag0), -1e9
    for mult in (0.5, 2.0 / 3.0, 1.0, 1.5, 2.0, 3.0):
        cand = lag0 * mult
        bpm_c = 60.0 / (cand * hop_dur)
        if bpm_c < bpm_lo - 1 or bpm_c > bpm_hi + 1 or cand >= len(ac) / 2:
            continue
        # Mild log-Gaussian tempo prior centered ~115 BPM: prefers the
        # musically-typical octave when comb evidence is comparable.
        prior = np.exp(-0.5 * (np.log2(bpm_c / 115.0) / 0.6) ** 2)
        s = comb(cand) * (0.6 + 0.4 * prior)
        if s > best_score:
            best_score, best_lag = s, cand
    lag = int(round(best_lag))
    # Parabolic interpolation for sub-lag precision.
    if 1 <= lag < len(ac) - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-12:
            lag = lag + 0.5 * (a - c) / denom
    return 60.0 / (lag * hop_dur)


def detect_beat_grid(onset, hop_dur, bpm):
    """Find the beat phase: offset maximizing onset energy under a beat comb."""
    period = 60.0 / bpm / hop_dur           # beat period in frames
    n = len(onset)
    best_off, best_score = 0.0, -1.0
    for off in np.linspace(0, period, 24, endpoint=False):
        idx = np.arange(off, n, period).astype(int)
        idx = idx[idx < n]
        score = onset[idx].sum()
        if score > best_score:
            best_score, best_off = score, off
    beat_frames = np.arange(best_off, n, period)
    return (beat_frames * hop_dur).tolist()  # beat times in seconds


def detect_key(mag, freqs):
    """Fold spectrum to chroma, correlate against K-S profiles."""
    valid = (freqs > 80) & (freqs < 2500)
    chroma = np.zeros(12)
    f = freqs[valid]
    m = mag[valid, :].sum(axis=1)
    midi = 69 + 12 * np.log2(f / 440.0)
    pc = np.round(midi).astype(int) % 12
    for k in range(12):
        chroma[k] = m[pc == k].sum()
    chroma = np.log1p(chroma)          # compress dynamics: pattern > loudness
    if chroma.sum() > 0:
        chroma = chroma / chroma.sum()

    best = (-2.0, "C", "major")
    for shift in range(12):
        rolled = np.roll(chroma, -shift)
        for mode, prof in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = np.corrcoef(rolled, prof)[0, 1]
            if r > best[0]:
                best = (r, PITCH_CLASSES[shift], mode)
    _, tonic, mode = best
    return {"tonic": tonic, "mode": mode, "camelot": CAMELOT[(tonic, mode)],
            "confidence": round(float(best[0]), 4)}


def frame_features(samples, sr, mag, freqs):
    """Per-hop RMS energy, spectral flux, bass-band energy."""
    hop = config.HOP_SIZE
    n_frames = mag.shape[1]
    rms = np.zeros(n_frames)
    for i in range(n_frames):
        seg = samples[i * hop: i * hop + config.FRAME_SIZE]
        rms[i] = np.sqrt(np.mean(seg ** 2)) if len(seg) else 0.0
    flux = _onset_strength(mag)
    bass = mag[(freqs >= 20) & (freqs <= 150), :].sum(axis=0)
    total = mag.sum(axis=0) + 1e-12
    return {
        "rms": rms.tolist(),
        "flux": flux.tolist(),
        "bass_ratio": (bass / total).tolist(),
        "hop_dur": config.HOP_SIZE / sr,
    }


def prefix_sums(frames):
    """Cumulative sums enabling O(1) window aggregates (Phase 1 step 5)."""
    return {
        "rms": np.concatenate([[0.0], np.cumsum(frames["rms"])]).tolist(),
        "flux": np.concatenate([[0.0], np.cumsum(frames["flux"])]).tolist(),
        "bass_ratio": np.concatenate([[0.0], np.cumsum(frames["bass_ratio"])]).tolist(),
    }


def window_mean(prefix, start, end):
    """O(1) mean over frame window [start, end) from a prefix-sum array."""
    end = max(end, start + 1)
    return (prefix[end] - prefix[start]) / (end - start)


def analyze(samples, sr=config.SAMPLE_RATE):
    """Full analysis pass. One call per track, at native tempo (Phase 1)."""
    freqs, _, mag = _stft_mag(samples, sr)
    hop_dur = config.HOP_SIZE / sr
    onset = _onset_strength(mag)
    bpm = detect_bpm(onset, hop_dur)
    beats = detect_beat_grid(onset, hop_dur, bpm)
    key = detect_key(mag, freqs)
    frames = frame_features(samples, sr, mag, freqs)
    return {
        "engine": "essentia" if HAVE_ESSENTIA else "fallback-dsp",
        "sr": sr,
        "duration_s": len(samples) / sr,
        "bpm": round(float(bpm), 2),
        "beat_grid": [round(b, 5) for b in beats],
        "key": key,
        "frames": frames,
        "prefix": prefix_sums(frames),
    }


def rescale_analysis(a, ratio):
    """Rescale time-domain analysis by a stretch ratio instead of re-running
    the full pass per variant (Phase 1 step 7). ratio = target_bpm / native_bpm;
    durations shrink by 1/ratio."""
    out = dict(a)
    out["bpm"] = round(a["bpm"] * ratio, 2)
    out["duration_s"] = a["duration_s"] / ratio
    out["beat_grid"] = [round(b / ratio, 5) for b in a["beat_grid"]]
    frames = dict(a["frames"])
    frames["hop_dur"] = a["frames"]["hop_dur"] / ratio
    out["frames"] = frames
    out["rescaled_from_native"] = True
    return out
