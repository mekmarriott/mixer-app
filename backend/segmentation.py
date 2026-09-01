"""Structural segmentation (Phase 1 step 4).

Novelty-curve approach: build a self-similarity novelty signal from smoothed
frame energy + flux, pick peaks as section boundaries, then label segments by
energy quantile + position heuristics (intro/outro by position, breakdown =
low-energy interior, drop = high-energy interior). Non-overlapping by
construction — used only as a coarse region filter; precise transition
candidates come from sliding windows (transitions.py)."""
import numpy as np
from scipy import signal as sp_signal


def _smooth(x, k):
    k = max(1, k)
    return np.convolve(x, np.ones(k) / k, mode="same")


def segment(analysis, min_segments=4):
    rms = np.asarray(analysis["frames"]["rms"])
    flux = np.asarray(analysis["frames"]["flux"])
    hop_dur = analysis["frames"]["hop_dur"]
    n = len(rms)
    if n < 16:
        return [{"label": "full", "start_s": 0.0, "end_s": analysis["duration_s"],
                 "start_frame": 0, "end_frame": n, "energy": float(rms.mean() if n else 0.0)}]

    feat = _smooth(rms / (rms.max() + 1e-12), int(0.5 / hop_dur)) \
        + 0.5 * _smooth(flux / (flux.max() + 1e-12), int(0.5 / hop_dur))

    # Novelty = |derivative| of the smoothed combined feature.
    novelty = np.abs(np.gradient(feat))
    novelty = _smooth(novelty, int(0.5 / hop_dur))

    min_gap = int(3.0 / hop_dur)   # sections at least 3s apart
    peaks, props = sp_signal.find_peaks(novelty, distance=max(1, min_gap))
    if len(peaks) > 0:
        order = np.argsort(props.get("peak_heights", novelty[peaks]))[::-1] \
            if "peak_heights" in props else np.argsort(novelty[peaks])[::-1]
        keep = sorted(peaks[order[:max(min_segments + 2, 7)]])
    else:
        keep = []

    bounds = [0] + [int(p) for p in keep if 0 < p < n - 1] + [n]
    bounds = sorted(set(bounds))

    segments = []
    energies = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        energies.append(rms[s:e].mean() if e > s else 0.0)
    if not energies:
        energies = [0.0]
    lo, hi = np.quantile(energies, 0.33), np.quantile(energies, 0.66)

    for i, (s, e) in enumerate(zip(bounds[:-1], bounds[1:])):
        en = float(energies[i])
        if i == 0:
            label = "intro"
        elif i == len(bounds) - 2:
            label = "outro"
        elif en <= lo:
            label = "breakdown"
        elif en >= hi:
            label = "drop"
        else:
            label = "verse"
        segments.append({
            "label": label,
            "start_s": round(s * hop_dur, 4),
            "end_s": round(e * hop_dur, 4),
            "start_frame": int(s),
            "end_frame": int(e),
            "energy": round(en, 6),
        })
    return segments


# Which roles make good exit (track A) / entry (track B) regions.
EXIT_ROLES = {"outro": 1.0, "breakdown": 0.85, "verse": 0.5, "drop": 0.25, "intro": 0.2}
ENTRY_ROLES = {"intro": 1.0, "breakdown": 0.85, "verse": 0.5, "drop": 0.3, "outro": 0.2}
