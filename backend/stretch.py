"""Offline pitch-preserving time-stretch (Phase 1 step 7).

Provider seam: uses the Rubber Band CLI when present (production path,
highest quality, GPL/commercial licensed — see requirements.md §3). Fallback
is an STFT phase vocoder (scipy) — adequate as a bare-install stand-in.
ratio = target_bpm / native_bpm; output duration = input / ratio.

Variants are pre-rendered once at ingestion, never in the playback path, so
CPU is cheap here — but spending it on the R3 engine makes the audio worse,
not better. See RUBBERBAND_ARGS.

Set DJMIXER_REQUIRE_RUBBERBAND=1 to make a missing Rubber Band a hard error
instead of a silent downgrade to the phase vocoder.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import config
from .audio_io import load_wav, save_wav

RUBBERBAND = shutil.which("rubberband")

# Stay on the CLI's default R2 engine. R3 ("-3"/"--fine") has the better
# general reputation and this pipeline used to pass it, but on our material it
# measures materially WORSE: over 4 percussive catalog tracks at ratios across
# the +/-10% stretch range, R2 retains 84% of the master's transient attack and
# 82% of its RMS, while R3 retains 67% and 73%. R3 smears energy out of the
# transients and into the gaps between them — the beat loses definition and the
# noise floor rises, which is what "washy" sounds like. It is not a sample-rate
# artefact: stretching at 44.1 kHz and resampling back down recovers none of it
# (69.4% vs 69.9% attack retained).
#
# Max crispness ("-c 6") on R2 sharpens attacks slightly further (84.9%) but
# costs 3 points of RMS, so it thins the sustain for no real transient gain.
#
# -q keeps the per-track progress bar out of ingestion logs.
RUBBERBAND_ARGS = ["-q"]


def require_rubberband():
    """True when the caller demanded the production stretch engine."""
    return os.environ.get("DJMIXER_REQUIRE_RUBBERBAND", "").lower() in ("1", "true", "yes")


if not RUBBERBAND and require_rubberband():   # pragma: no cover - install-dependent
    raise RuntimeError(
        "DJMIXER_REQUIRE_RUBBERBAND is set but the `rubberband` CLI is not on PATH")


def stretch(samples, sr, ratio):
    """Return samples time-stretched so tempo scales by `ratio`, pitch preserved."""
    if abs(ratio - 1.0) < 1e-6:
        return np.array(samples, dtype=np.float64)
    if RUBBERBAND:
        return _stretch_rubberband(samples, sr, ratio)
    return _stretch_phase_vocoder(samples, sr, ratio)


def _stretch_rubberband(samples, sr, ratio):
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
        save_wav(src, samples, sr)
        # rubberband --time takes a duration multiplier (1/ratio for tempo ratio)
        proc = subprocess.run(
            [RUBBERBAND, *RUBBERBAND_ARGS, "--time", str(1.0 / ratio),
             str(src), str(dst)],
            capture_output=True, text=True)
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError(
                f"rubberband failed (exit {proc.returncode}) at ratio {ratio:.4f}: "
                f"{proc.stderr.strip()[:400]}")
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
