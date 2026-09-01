"""Offline pitch-preserving time-stretch (Phase 1 step 7).

Provider seam: uses the Rubber Band CLI when present (production path,
highest quality, GPL/commercial licensed — see requirements.md §3). Fallback
is an STFT phase vocoder (scipy) — adequate for the prototype and fully
offline. ratio = target_bpm / native_bpm; output duration = input / ratio.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy import signal as sp_signal

from . import config
from .audio_io import load_wav, save_wav

RUBBERBAND = shutil.which("rubberband")


def stretch(samples, sr, ratio):
    """Return samples time-stretched so tempo scales by `ratio`, pitch preserved."""
    if abs(ratio - 1.0) < 1e-6:
        return np.array(samples, dtype=np.float64)
    if RUBBERBAND:
        return _stretch_rubberband(samples, sr, ratio)
    return _stretch_phase_vocoder(samples, sr, ratio)


def _stretch_rubberband(samples, sr, ratio):  # pragma: no cover - CLI absent here
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
        save_wav(src, samples, sr)
        # rubberband --time takes a duration multiplier (1/ratio for tempo ratio)
        subprocess.run([RUBBERBAND, "--time", str(1.0 / ratio), str(src), str(dst)],
                       check=True, capture_output=True)
        out, _ = load_wav(dst)
        return out


def _stretch_phase_vocoder(samples, sr, ratio):
    """Classic phase vocoder: STFT, resample frame positions by `ratio`,
    accumulate phase by per-bin instantaneous frequency, ISTFT."""
    n_fft = config.FRAME_SIZE
    hop = config.HOP_SIZE
    window = np.hanning(n_fft)

    # Analysis STFT
    n_frames = 1 + max(0, (len(samples) - n_fft)) // hop
    if n_frames < 2:
        return np.array(samples)
    stft = np.empty((n_fft // 2 + 1, n_frames), dtype=np.complex128)
    for i in range(n_frames):
        seg = samples[i * hop: i * hop + n_fft]
        if len(seg) < n_fft:
            seg = np.pad(seg, (0, n_fft - len(seg)))
        stft[:, i] = np.fft.rfft(seg * window)

    # Synthesis frame positions in analysis-frame coordinates
    steps = np.arange(0, n_frames - 1, ratio)
    mag = np.abs(stft)
    phase = np.angle(stft)
    bin_freqs = 2 * np.pi * np.arange(n_fft // 2 + 1) * hop / n_fft

    out_phase = phase[:, 0].copy()
    out = np.zeros(int(len(steps) * hop + n_fft))
    for k, s in enumerate(steps):
        i = int(s)
        frac = s - i
        m = (1 - frac) * mag[:, i] + frac * mag[:, min(i + 1, n_frames - 1)]
        spec = m * np.exp(1j * out_phase)
        frame = np.fft.irfft(spec) * window
        pos = k * hop
        out[pos:pos + n_fft] += frame
        # Phase advance from instantaneous frequency between analysis frames
        dp = phase[:, min(i + 1, n_frames - 1)] - phase[:, i] - bin_freqs
        dp = dp - 2 * np.pi * np.round(dp / (2 * np.pi))
        out_phase = out_phase + bin_freqs + dp

    peak = np.max(np.abs(out))
    if peak > 1e-9:
        out = out / peak * (np.max(np.abs(samples)) or 0.9)
    return out


def engine_name():
    return "rubberband" if RUBBERBAND else "phase-vocoder"
