"""Audio analysis (Phase 1).

Provider seam. `analyze()` routes to Essentia when the package is importable
(the production path); otherwise an equivalent numpy/scipy implementation
serves the same contract so the pipeline still runs on a bare install.

                 essentia path                     fallback path
  BPM            RhythmExtractor2013 multifeature  onset autocorrelation
  beat grid      same (real tracked beat times)    comb-phase search
  key            KeyExtractor (edma profile)       chroma x Krumhansl-Schmuckler
  frames         Spectrum / RMS / Flux per hop     scipy STFT per hop
  prefix sums    cumulative sums over frame features (shared)

Set DJMIXER_REQUIRE_ESSENTIA=1 to make a missing/broken Essentia a hard error
instead of a silent downgrade — the fallback is a different engine, and a
production ingest should never quietly use it.

Output is a plain dict (JSON-serializable) cached by the ingestion pipeline.
"""
import os

import numpy as np

from . import config

# scipy is imported lazily inside _stft_mag rather than at module scope.
# matching.py imports window_mean from here and app.py imports
# rescale_analysis, so a top-level scipy import would pull the whole scipy tree
# into every API cold start — against a 250 MB serverless bundle limit, for
# code the request path never executes. Only _stft_mag (ingest-time) needs it.
# See docs/infrastructure-plan.md §1.3.

# Essentia's rhythm/key extractors carry 44.1 kHz assumptions in their internal
# frame sizes and tuning; several (RhythmExtractor2013 among them) expose no
# sampleRate parameter at all. Feeding them our 22.05 kHz masters directly makes
# them read the audio as half-length/double-tempo, so analysis input is
# resampled to this rate first. Storage/variants stay at config.SAMPLE_RATE.
ANALYSIS_SR = 44100

ESSENTIA_KEY_PROFILE = "edma"   # profile trained on electronic dance music

try:
    import essentia
    import essentia.standard as es
    essentia.log.warningActive = False      # per-frame notices, not actionable
    essentia.log.infoActive = False
    HAVE_ESSENTIA = True
    ESSENTIA_ERROR = None
except Exception as exc:                    # pragma: no cover - install-dependent
    HAVE_ESSENTIA = False
    ESSENTIA_ERROR = exc


def require_essentia():
    """True when the caller demanded the production analysis engine."""
    return os.environ.get("DJMIXER_REQUIRE_ESSENTIA", "").lower() in ("1", "true", "yes")


if not HAVE_ESSENTIA and require_essentia():   # pragma: no cover - install-dependent
    raise ImportError(
        f"DJMIXER_REQUIRE_ESSENTIA is set but essentia is unavailable: {ESSENTIA_ERROR}")


def engine_name():
    return "essentia" if HAVE_ESSENTIA else "fallback-dsp"

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

# Essentia names some tonics with flats; CAMELOT is keyed by sharps.
ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


# --------------------------------------------------------------- essentia path

def _resample_for_essentia(samples, sr):
    """Mono float32 at ANALYSIS_SR — the rate Essentia's extractors assume."""
    x = np.ascontiguousarray(np.asarray(samples, dtype=np.float32))
    if int(sr) == ANALYSIS_SR:
        return x
    return es.Resample(inputSampleRate=int(sr), outputSampleRate=ANALYSIS_SR)(x)


def _essentia_rhythm(x44):
    """BPM + tracked beat times (seconds) from RhythmExtractor2013.

    'multifeature' agrees five beat trackers and reports a confidence, which is
    the most reliable of Essentia's tempo estimators on full mixes."""
    bpm, beats, confidence, _estimates, _intervals = \
        es.RhythmExtractor2013(method="multifeature")(x44)
    return float(bpm), [float(b) for b in beats], float(confidence)


def _essentia_key(x44):
    tonic, scale, strength = es.KeyExtractor(
        profileType=ESSENTIA_KEY_PROFILE, sampleRate=ANALYSIS_SR)(x44)
    tonic = ENHARMONIC.get(tonic, tonic)
    if (tonic, scale) not in CAMELOT:
        raise ValueError(f"Essentia returned an unmappable key: {tonic} {scale}")
    return {"tonic": tonic, "mode": scale, "camelot": CAMELOT[(tonic, scale)],
            "confidence": round(float(strength), 4)}


def _essentia_frames(samples, sr):
    """Per-hop RMS, spectral flux and bass-band ratio, computed by Essentia.

    Runs at the native storage rate so `hop_dur` stays config.HOP_SIZE / sr and
    every downstream frame index (segments, prefix sums, transition windows)
    keeps its existing meaning."""
    x = np.ascontiguousarray(np.asarray(samples, dtype=np.float32))
    window = es.Windowing(type="hann", size=config.FRAME_SIZE)
    spectrum = es.Spectrum(size=config.FRAME_SIZE)
    rms_of = es.RMS()
    # halfRectify + L1 == "positive magnitude increases summed over bins",
    # the same novelty definition the fallback uses.
    flux_of = es.Flux(halfRectify=True, norm="L1")

    rms, flux, mags = [], [], []
    for frame in es.FrameGenerator(x, frameSize=config.FRAME_SIZE,
                                   hopSize=config.HOP_SIZE, startFromZero=True):
        spec = spectrum(window(frame))
        rms.append(float(rms_of(frame)))
        flux.append(float(flux_of(spec)))
        mags.append(spec)

    if not mags:
        return {"rms": [], "flux": [], "bass_ratio": [],
                "hop_dur": config.HOP_SIZE / sr}

    mag = np.asarray(mags).T                       # (bins, frames)
    freqs = np.linspace(0.0, sr / 2.0, mag.shape[0])
    bass = mag[(freqs >= 20) & (freqs <= 150), :].sum(axis=0)
    total = mag.sum(axis=0) + 1e-12
    return {
        "rms": rms,
        "flux": flux,
        "bass_ratio": (bass / total).tolist(),
        "hop_dur": config.HOP_SIZE / sr,
    }


def _analyze_essentia(samples, sr):
    x44 = _resample_for_essentia(samples, sr)
    bpm, beats, confidence = _essentia_rhythm(x44)
    key = _essentia_key(x44)
    frames = _essentia_frames(samples, sr)
    return {
        "engine": "essentia",
        "engine_version": essentia.__version__,
        "sr": sr,
        "analysis_sr": ANALYSIS_SR,
        "duration_s": len(samples) / sr,
        "bpm": round(bpm, 2),
        "bpm_confidence": round(confidence, 4),
        "beat_grid": [round(b, 5) for b in beats],
        "key": key,
        "frames": frames,
        "prefix": prefix_sums(frames),
    }


# --------------------------------------------------------------- fallback path

def _stft_mag(samples, sr):
    from scipy import signal as sp_signal      # lazy: see module header
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


def _analyze_fallback(samples, sr):
    freqs, _, mag = _stft_mag(samples, sr)
    hop_dur = config.HOP_SIZE / sr
    onset = _onset_strength(mag)
    bpm = detect_bpm(onset, hop_dur)
    beats = detect_beat_grid(onset, hop_dur, bpm)
    key = detect_key(mag, freqs)
    frames = frame_features(samples, sr, mag, freqs)
    return {
        "engine": "fallback-dsp",
        "sr": sr,
        "duration_s": len(samples) / sr,
        "bpm": round(float(bpm), 2),
        "beat_grid": [round(b, 5) for b in beats],
        "key": key,
        "frames": frames,
        "prefix": prefix_sums(frames),
    }


def analyze(samples, sr=config.SAMPLE_RATE):
    """Full analysis pass. One call per track, at native tempo (Phase 1).

    Uses Essentia when installed. If Essentia is present but throws on a
    particular track, that is surfaced rather than swallowed when
    DJMIXER_REQUIRE_ESSENTIA is set; otherwise the fallback engine takes over
    and `engine` records which one actually ran."""
    if HAVE_ESSENTIA:
        try:
            return _analyze_essentia(samples, sr)
        except Exception:                   # pragma: no cover - engine-specific
            if require_essentia():
                raise
    return _analyze_fallback(samples, sr)


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
