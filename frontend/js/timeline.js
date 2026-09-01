// Track Window renderer + pointer interaction (ui-requirements §1, §2, §5).
//
// Canvas draws: waveforms (track 1 magenta / track 2 blue), the overlap zone
// with both waveforms interleaved and amplitude-scaled by the REAL crossfade
// gain (crossfade.js — same curves audio.js schedules), the gold marker lane
// (arrow size = score), the beat grid, and the player cursor.

import { trackGainAt } from "./crossfade.js";
import { markerSizePx } from "./align.js";
import { timeToPx, pxToTime } from "./navbar.js";
import { overlapZone } from "./state.js";

export const COLORS = {
  track1: "#ff4fa3",
  track2: "#4fa8ff",
  marker: "#ffc24b",
  cursor: "#e8e6df",
  beat: "rgba(139,143,163,0.16)",
  downbeat: "rgba(139,143,163,0.34)",
  overlay: "rgba(255,194,75,0.07)",
};

const MARKER_LANE_H = 30;

export class Timeline {
  constructor(canvas, vp) {
    this.canvas = canvas;
    this.vp = vp;             // navbar.js viewport (shared)
    this.mix = null;
    this.waveforms = new Map(); // trackId -> {points, duration_s, beat_grid}
    this.markers = [];
    this.cursor = 0;
    this.hoverMarker = null;
    this.dpr = window.devicePixelRatio || 1;
  }

  setMix(mix) { this.mix = mix; }
  setWaveform(id, wf) { this.waveforms.set(id, wf); }
  setMarkers(m) { this.markers = m || []; }
  setCursor(t) { this.cursor = t; }

  resize() {
    const r = this.canvas.getBoundingClientRect();
    this.canvas.width = r.width * this.dpr;
    this.canvas.height = r.height * this.dpr;
  }

  draw() {
    const ctx = this.canvas.getContext("2d");
    const W = this.canvas.width, H = this.canvas.height;
    const dpr = this.dpr;
    ctx.clearRect(0, 0, W, H);
    if (!this.mix) return;
    const laneH = MARKER_LANE_H * dpr;
    const waveTop = laneH;
    const waveH = H - laneH;
    const overlap = overlapZone(this.mix);

    // Beat grid of track 1 (mix reference grid), P4-24 visual aid.
    const ref = this.mix.tracks[0] && this.waveforms.get(this.mix.tracks[0].id);
    if (ref?.beat_grid) {
      ref.beat_grid.forEach((bt, i) => {
        const t = bt + this.mix.tracks[0].offset;
        const x = timeToPx(this.vp, t, W);
        if (x < 0 || x > W) return;
        ctx.strokeStyle = i % 4 === 0 ? COLORS.downbeat : COLORS.beat;
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath(); ctx.moveTo(x, waveTop); ctx.lineTo(x, H); ctx.stroke();
      });
    }

    // Overlap zone shading.
    if (overlap) {
      const x0 = timeToPx(this.vp, overlap.start, W);
      const x1 = timeToPx(this.vp, overlap.end, W);
      ctx.fillStyle = COLORS.overlay;
      ctx.fillRect(x0, waveTop, x1 - x0, waveH);
    }

    // Waveforms: half-height each around their own centerline; both tracks
    // share the full band so overlapping regions visually interleave.
    this.mix.tracks.forEach((track, idx) => {
      const wf = this.waveforms.get(track.id);
      if (!wf) return;
      const color = idx === 0 ? COLORS.track1 : COLORS.track2;
      const role = idx === 0 ? "out" : "in";
      const mid = waveTop + waveH / 2;
      const amp = waveH * 0.46;
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.85;
      const n = wf.points.length;
      for (let i = 0; i < n; i++) {
        const tLocal = (i / (n - 1)) * wf.duration_s;
        const t = tLocal + track.offset;
        const x = timeToPx(this.vp, t, W);
        if (x < -4 || x > W + 4) continue;
        // Amplitude scaled by the real crossfade gain — truthful fade (P4-25).
        const g = trackGainAt(t, overlap, role);
        const h = Math.max(1, wf.points[i] * amp * g);
        const barW = Math.max(1, (W / this.vp.dur) * (wf.duration_s / n) * 0.7);
        ctx.fillRect(x, mid - h, barW, h * 2);
      }
      ctx.globalAlpha = 1;
    });

    // Marker lane (P4-20, P4-21): gold arrows atop the window, size = score.
    ctx.strokeStyle = "rgba(255,194,75,0.25)";
    ctx.lineWidth = 1 * dpr;
    ctx.beginPath(); ctx.moveTo(0, laneH - 0.5 * dpr); ctx.lineTo(W, laneH - 0.5 * dpr); ctx.stroke();
    const aOffset = this.mix.tracks[0]?.offset ?? 0;
    for (const m of this.markers) {
      const t = m.a_start_s + aOffset;
      const x = timeToPx(this.vp, t, W);
      if (x < 0 || x > W) continue;
      const size = markerSizePx(m.score) * dpr;
      ctx.fillStyle = COLORS.marker;
      ctx.globalAlpha = m === this.hoverMarker ? 1 : 0.85;
      ctx.beginPath();
      ctx.moveTo(x, laneH - 2 * dpr);
      ctx.lineTo(x - size * 0.38, laneH - 2 * dpr - size);
      ctx.lineTo(x + size * 0.38, laneH - 2 * dpr - size);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // Player cursor (P4-05..07).
    const cx = timeToPx(this.vp, this.cursor, W);
    if (cx >= 0 && cx <= W) {
      ctx.strokeStyle = COLORS.cursor;
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, H); ctx.stroke();
      ctx.fillStyle = COLORS.cursor;
      ctx.beginPath();
      ctx.moveTo(cx - 5 * dpr, 0); ctx.lineTo(cx + 5 * dpr, 0); ctx.lineTo(cx, 7 * dpr);
      ctx.closePath(); ctx.fill();
    }
  }

  // Hit-testing helpers used by app.js pointer handlers.
  pxToTimeLocal(px) { return pxToTime(this.vp, px * this.dpr, this.canvas.width); }
  trackAtPoint(px, py) {
    if (!this.mix) return null;
    const t = this.pxToTimeLocal(px);
    // Prefer track 2 (draggable) when both overlap under the pointer.
    for (let i = this.mix.tracks.length - 1; i >= 0; i--) {
      const tr = this.mix.tracks[i];
      if (t >= tr.offset && t <= tr.offset + tr.duration) return tr;
    }
    return null;
  }
}
