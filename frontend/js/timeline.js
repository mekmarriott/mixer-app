// Track Window renderer + pointer interaction (ui-requirements §1, §2, §5).
//
// Canvas draws: waveforms (track 1 magenta / track 2 blue), the overlap zone
// with both waveforms interleaved and amplitude-scaled by the REAL crossfade
// gain (crossfade.js — same curves audio.js schedules), the gold marker lane
// (arrow size = score), the beat grid, and the player cursor.

import { trackGainAt } from "./crossfade.js";
import { markerSizePx, markerToOffset } from "./align.js";
import { timeToPx, pxToTime } from "./navbar.js";
import { offsets, overlapZones, overlapsFor } from "./state.js";

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
// Playhead handle, in CSS px. GRAB_PX is how close the pointer must be
// horizontally to grab the playhead instead of the track underneath.
const CURSOR_HANDLE_W = 13;
const CURSOR_HANDLE_H = 12;
export const CURSOR_GRAB_PX = 9;
/** The strip along the top: markers plus the playhead ruler, never tracks. */
export const RULER_H = MARKER_LANE_H;
const DELETE_BADGE = 16;          // room reserved for the x, in CSS px
export const BADGE_GRAB_PX = 10;

// A 100-track mix needs more than the two mandated hues. Magenta and blue stay
// first and second (ui-requirements mandates them for tracks 1 and 2); the rest
// continue around the wheel, skipping gold, which is reserved for markers.
export const TRACK_COLORS = [
  "#ff4fa3", "#4fa8ff", "#5ee6a8", "#c98bff", "#ff8f5e", "#4fd6e6",
  "#e6d24f", "#8b9bff", "#ff6fd8", "#7ee65e",
];

/** Colour for the nth track in the chain. Adjacent tracks never share one. */
export function trackColor(index) {
  return TRACK_COLORS[index % TRACK_COLORS.length];
}

export class Timeline {
  constructor(canvas, vp) {
    this.canvas = canvas;
    this.vp = vp;             // navbar.js viewport (shared)
    this.mix = null;
    this.waveforms = new Map(); // trackId -> {points, duration_s, beat_grid}
    this.markerGroups = [];
    this.selected = null;      // index of the selected track, or null
    this.cursor = 0;
    this.hoverMarker = null;
    this.dpr = window.devicePixelRatio || 1;
  }

  setMix(mix) { this.mix = mix; }
  setWaveform(id, wf) { this.waveforms.set(id, wf); }
  /**
   * Marker groups, one per junction: `[{ origin, markers }]` where `origin` is
   * the absolute start of the junction's LEFT track (marker `a_start_s` is
   * relative to it).
   *
   * A chain of N tracks has N-1 junctions and every one of them has candidate
   * transition points. Holding a single set meant adding a track silently
   * erased the previous junction's markers.
   */
  setMarkerGroups(groups) { this.markerGroups = groups || []; }
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
    const offs = offsets(this.mix);
    const zones = overlapZones(this.mix);

    // Beat grid of track 1 (mix reference grid), P4-24 visual aid.
    const ref = this.mix.tracks[0] && this.waveforms.get(this.mix.tracks[0].id);
    if (ref?.beat_grid) {
      ref.beat_grid.forEach((bt, i) => {
        const t = bt + (offs[0] ?? 0);
        const x = timeToPx(this.vp, t, W);
        if (x < 0 || x > W) return;
        ctx.strokeStyle = i % 4 === 0 ? COLORS.downbeat : COLORS.beat;
        ctx.lineWidth = 1 * dpr;
        ctx.beginPath(); ctx.moveTo(x, waveTop); ctx.lineTo(x, H); ctx.stroke();
      });
    }

    // Every transition zone in the chain gets its shading.
    ctx.fillStyle = COLORS.overlay;
    for (const z of zones) {
      const x0 = timeToPx(this.vp, z.start, W);
      const x1 = timeToPx(this.vp, z.end, W);
      if (x1 < 0 || x0 > W) continue;
      ctx.fillRect(x0, waveTop, x1 - x0, waveH);
    }

    // Waveforms: half-height each around their own centerline; both tracks
    // share the full band so overlapping regions visually interleave.
    this.mix.tracks.forEach((track, idx) => {
      const wf = this.waveforms.get(track.id);
      if (!wf) return;
      const start = offs[idx];
      // Skip tracks entirely outside the viewport — a 100-track mix draws only
      // what is on screen.
      if (start > this.vp.start + this.vp.dur || start + track.duration < this.vp.start) return;
      const color = trackColor(idx);
      const overlaps = overlapsFor(this.mix, idx);
      const mid = waveTop + waveH / 2;
      const amp = waveH * 0.46;
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.85;
      const n = wf.points.length;
      for (let i = 0; i < n; i++) {
        const tLocal = (i / (n - 1)) * wf.duration_s;
        const t = tLocal + start;
        const x = timeToPx(this.vp, t, W);
        if (x < -4 || x > W + 4) continue;
        // Amplitude scaled by the real crossfade gain — truthful fade (P4-25).
        const g = trackGainAt(t, overlaps);
        const h = Math.max(1, wf.points[i] * amp * g);
        const barW = Math.max(1, (W / this.vp.dur) * (wf.duration_s / n) * 0.7);
        ctx.fillRect(x, mid - h, barW, h * 2);
      }
      ctx.globalAlpha = 1;
    });

    // Track name along the foot of each waveform, so what is playing at any
    // point on the grid is readable without cross-referencing the deck.
    ctx.font = `${11 * dpr}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.textBaseline = "alphabetic";
    this.mix.tracks.forEach((track, idx) => {
      if (!this.waveforms.has(track.id)) return;
      const start = offs[idx];
      const end = start + track.duration;
      if (start > this.vp.start + this.vp.dur || end < this.vp.start) return;

      // Pin the label to the visible part of the track, so a track scrolled
      // half off-screen still shows its name.
      const x0 = Math.max(timeToPx(this.vp, start, W), 0);
      const x1 = Math.min(timeToPx(this.vp, end, W), W);
      const room = x1 - x0;
      if (room < 34 * dpr) return;             // too narrow to label legibly

      const selected = this.selected === idx;
      if (selected) {
        // Outline the selected track so it is obvious what Delete will remove.
        ctx.strokeStyle = trackColor(idx);
        ctx.lineWidth = 1.5 * dpr;
        ctx.setLineDash([4 * dpr, 3 * dpr]);
        ctx.strokeRect(x0 + 1, waveTop + 1, x1 - x0 - 2, waveH - 2);
        ctx.setLineDash([]);
      }

      const badge = selected ? DELETE_BADGE : 0;
      const label = this._fit(ctx, track.name || track.id,
                              room - (12 + badge) * dpr);
      if (!label) return;
      const tx = x0 + 6 * dpr;
      const ty = H - 7 * dpr;
      // Shadow first: names sit over the waveform and must stay readable.
      ctx.fillStyle = "rgba(12,14,20,0.85)";
      const tw = ctx.measureText(label).width;
      ctx.fillRect(tx - 3 * dpr, ty - 11 * dpr,
                   tw + (6 + badge) * dpr, 15 * dpr);
      ctx.fillStyle = trackColor(idx);
      ctx.fillText(label, tx, ty);

      if (selected) {
        // A visible target beats a hidden shortcut; the Delete key also works.
        const bx = tx + tw + 8 * dpr;
        ctx.strokeStyle = "#ff6b6b";
        ctx.lineWidth = 1.6 * dpr;
        const r = 4 * dpr;
        ctx.beginPath();
        ctx.moveTo(bx - r, ty - 4 * dpr - r); ctx.lineTo(bx + r, ty - 4 * dpr + r);
        ctx.moveTo(bx + r, ty - 4 * dpr - r); ctx.lineTo(bx - r, ty - 4 * dpr + r);
        ctx.stroke();
        this._deleteBadge = { idx, x: bx / dpr, y: (ty - 4 * dpr) / dpr };
      }
    });
    if (this.selected === null) this._deleteBadge = null;

    // Marker lane (P4-20, P4-21): gold arrows atop the window, size = score.
    ctx.strokeStyle = "rgba(255,194,75,0.25)";
    ctx.lineWidth = 1 * dpr;
    ctx.beginPath(); ctx.moveTo(0, laneH - 0.5 * dpr); ctx.lineTo(W, laneH - 0.5 * dpr); ctx.stroke();
    for (const group of this.markerGroups) {
      const origin = group.origin ?? 0;
      // Size relative to the candidates within THIS junction: that is the
      // comparison a user makes ("which of these is the best exit from this
      // track?"). Normalising across junctions would compare unlike pairs.
      const scoreSet = group.markers.map((m) => m.score);
      for (const m of group.markers) {
        // Draw where the NEXT TRACK would start, not where the transition
        // begins inside the previous one. `a_start_s` is the exit point in
        // track A; the incoming track has to begin `b_start_s` earlier so its
        // own entry point lands on that exit. ui-requirements.md calls these
        // "candidate transition start points for Track 2", and both the drop
        // snap and the drag snap already resolve to markerToOffset — only the
        // rendering disagreed, so an arrow never sat where a track landed.
        const x = timeToPx(this.vp, markerToOffset(m) + origin, W);
        if (x < 0 || x > W) continue;
        const size = markerSizePx(m.score, 10, 26, scoreSet) * dpr;
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
    }

    // Player cursor (P4-06, P4-07). The handle is deliberately chunky: it is a
    // drag target, and a 1px line is not one.
    const cx = timeToPx(this.vp, this.cursor, W);
    if (cx >= -CURSOR_HANDLE_W * dpr && cx <= W + CURSOR_HANDLE_W * dpr) {
      ctx.strokeStyle = COLORS.cursor;
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, H); ctx.stroke();
      ctx.fillStyle = this.cursorHot ? "#ffffff" : COLORS.cursor;
      const hw = (CURSOR_HANDLE_W / 2) * dpr;
      const hh = CURSOR_HANDLE_H * dpr;
      // Pentagon: a flat cap that is easy to hit, tapering to the exact time.
      ctx.beginPath();
      ctx.moveTo(cx - hw, 0);
      ctx.lineTo(cx + hw, 0);
      ctx.lineTo(cx + hw, hh * 0.6);
      ctx.lineTo(cx, hh);
      ctx.lineTo(cx - hw, hh * 0.6);
      ctx.closePath(); ctx.fill();
    }
  }

  /** Truncate to fit `maxW`, with an ellipsis when it does not. */
  _fit(ctx, text, maxW) {
    if (maxW <= 0) return "";
    if (ctx.measureText(text).width <= maxW) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (ctx.measureText(text.slice(0, mid) + "\u2026").width <= maxW) lo = mid;
      else hi = mid - 1;
    }
    return lo > 0 ? text.slice(0, lo) + "\u2026" : "";
  }

  // Hit-testing helpers used by app.js pointer handlers.
  pxToTimeLocal(px) { return pxToTime(this.vp, px * this.dpr, this.canvas.width); }

  /** Is the pointer on the selected track's delete badge? */
  deleteBadgeAtPoint(px, py) {
    const b = this._deleteBadge;
    if (!b) return null;
    return (Math.abs(px - b.x) <= BADGE_GRAB_PX &&
            Math.abs(py - b.y) <= BADGE_GRAB_PX) ? b.idx : null;
  }

  /**
   * Is the pointer on the playhead?
   *
   * Checked BEFORE track hit-testing, so the playhead stays reachable even
   * when it sits over a track — which, once a mix has tracks, is everywhere.
   */
  cursorAtPoint(px) {
    const cx = timeToPx(this.vp, this.cursor, this.canvas.width) / this.dpr;
    return Math.abs(px - cx) <= CURSOR_GRAB_PX;
  }
  trackAtPoint(px, py) {
    if (!this.mix) return null;
    // The top lane belongs to the markers and the playhead ruler. Ignoring py
    // meant a click up there grabbed the track underneath, which made the
    // playhead impossible to reach.
    if (py != null && py < MARKER_LANE_H) return null;
    const t = this.pxToTimeLocal(px);
    const offs = offsets(this.mix);
    // Later track wins where two overlap: in a chain the newer one sits on top.
    for (let i = this.mix.tracks.length - 1; i >= 0; i--) {
      if (t >= offs[i] && t <= offs[i] + this.mix.tracks[i].duration) return i;
    }
    return null;
  }
}
