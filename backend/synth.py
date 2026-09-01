"""Deterministic synthetic track generator (offline provider).

Generates beat-driven audio with DJ-typical structure — intro, build, drop,
breakdown, outro — so BPM detection, key detection, segmentation, and
transition scoring all operate on genuinely structured signal rather than
noise. Deterministic per track id (seeded), so tests are reproducible.
"""
import hashlib

import numpy as np

from . import config

# Camelot -> (tonic pitch class, mode). Minor = 'A', Major = 'B'.
CAMELOT_TONIC = {
    "1A": (8, "minor"), "2A": (3, "minor"), "3A": (10, "minor"), "4A": (5, "minor"),
    "5A": (0, "minor"), "6A": (7, "minor"), "7A": (2, "minor"), "8A": (9, "minor"),
    "9A": (4, "minor"), "10A": (11, "minor"), "11A": (6, "minor"), "12A": (1, "minor"),
    "1B": (11, "major"), "2B": (6, "major"), "3B": (1, "major"), "4B": (8, "major"),
    "5B": (3, "major"), "6B": (10, "major"), "7B": (5, "major"), "8B": (0, "major"),
    "9B": (7, "major"), "10B": (2, "major"), "11B": (9, "major"), "12B": (4, "major"),
}

MINOR_DEGREES = [0, 2, 3, 5, 7, 8, 10]
MAJOR_DEGREES = [0, 2, 4, 5, 7, 9, 11]

# Section layout as fractions of total duration, with an energy level per section.
SECTIONS = [
    ("intro", 0.00, 0.12, 0.35),
    ("build", 0.12, 0.30, 0.65),
    ("drop", 0.30, 0.55, 1.00),
    ("breakdown", 0.55, 0.70, 0.40),
    ("drop2", 0.70, 0.88, 0.95),
    ("outro", 0.88, 1.00, 0.30),
]


def _pc_freq(pitch_class, octave=3):
    # A4 = 440 Hz = pitch class 9, octave 4
    midi = 12 * (octave + 1) + pitch_class
    return 440.0 * 2 ** ((midi - 69) / 12)


def synthesize(track_spec, sr=config.SAMPLE_RATE):
    """Return mono float64 samples for a synthetic track spec."""
    seed = int(hashlib.sha256(track_spec["id"].encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)

    dur = float(track_spec.get("duration_s", 60))
    bpm = float(track_spec["bpm"])
    tonic, mode = CAMELOT_TONIC[track_spec["key"]]
    degrees = MINOR_DEGREES if mode == "minor" else MAJOR_DEGREES

    n = int(dur * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)

    beat_period = 60.0 / bpm
    beat_times = np.arange(0, dur, beat_period)

    # Per-sample energy envelope from section layout (smoothed).
    env = np.ones(n) * 0.3
    for _, f0, f1, level in SECTIONS:
        env[int(f0 * n):int(f1 * n)] = level
    kernel = int(0.25 * sr)
    env = np.convolve(env, np.ones(kernel) / kernel, mode="same")

    # Kick drum on every beat: decaying sine burst pitched to the tonic so
    # low-end harmonics reinforce (not pollute) the key.
    kick_len = int(0.10 * sr)
    kick_t = np.arange(kick_len) / sr
    kick_freq = _pc_freq(tonic, octave=1)
    kick = np.sin(2 * np.pi * kick_freq * kick_t) * np.exp(-kick_t * 35)
    for bt in beat_times:
        i = int(bt * sr)
        if i + kick_len < n:
            out[i:i + kick_len] += kick * env[i]

    # Hi-hat on offbeats: short noise burst (adds spectral flux for onset detection).
    hat_len = int(0.03 * sr)
    hat = rng.standard_normal(hat_len) * np.exp(-np.arange(hat_len) / sr * 120)
    for bt in beat_times + beat_period / 2:
        i = int(bt * sr)
        if i + hat_len < n:
            out[i:i + hat_len] += 0.25 * hat * env[i]

    # Bassline: root/fifth alternating per beat, in key.
    bass_f = _pc_freq(tonic, octave=2)
    fifth_f = _pc_freq((tonic + 7) % 12, octave=2)
    bass_wave = np.where((t // beat_period).astype(int) % 4 == 3,
                         np.sin(2 * np.pi * fifth_f * t),
                         np.sin(2 * np.pi * bass_f * t))
    out += 0.30 * bass_wave * env

    # Chord pad: sustained triad degrees, changing every 4 beats. Gives chroma
    # content for key detection.
    bar = 4 * beat_period
    chord_idx = (t // bar).astype(int)
    pad = np.zeros(n)
    deg_arr = np.array(degrees)
    pc_freqs4 = np.array([_pc_freq(pc, octave=4) for pc in range(12)])
    chord_roots = np.array([0, 0, 3, 4])          # i, i, iv, v — tonic-centered
    root_idx = chord_roots[chord_idx % 4]
    for step in range(3):
        deg = deg_arr[(root_idx + step * 2) % 7]
        pad += np.sin(2 * np.pi * pc_freqs4[(tonic + deg) % 12] * t + rng.uniform(0, 2 * np.pi))
    # Tonic + scale-third drone anchors both key *and mode* for
    # chroma-based detection (the third is what separates major from minor).
    third = deg_arr[2]
    pad += 0.8 * np.sin(2 * np.pi * pc_freqs4[tonic] * t)
    pad += 0.6 * np.sin(2 * np.pi * pc_freqs4[(tonic + third) % 12] * t)
    out += 0.12 * pad * env

    # Melody line during high-energy sections only: scale notes per half-beat.
    mel_idx = (t // (beat_period / 2)).astype(int)
    mel_deg = np.array(degrees)[(mel_idx * 3) % 7]
    pc_freqs5 = np.array([_pc_freq(pc, octave=5) for pc in range(12)])
    mel = np.sin(2 * np.pi * pc_freqs5[(tonic + mel_deg) % 12] * t)
    out += 0.10 * mel * (env > 0.7) * env

    out += 0.005 * rng.standard_normal(n)   # noise floor
    peak = np.max(np.abs(out))
    return (out / peak * 0.9) if peak > 0 else out
