// Web Audio playback engine (P4-01, P4-02, P4-25).
//
// Both tracks are pre-rendered variants at the SAME grid BPM — no live
// time-stretch happens here (project plan Phase 4). The crossfade schedules
// gain values sampled from crossfade.js, the same module the timeline uses
// to draw the fade, so sight and sound cannot drift apart.

import { gainCurve } from "./crossfade.js";
import { api } from "./api.js";

export class Player {
  constructor() {
    this.ctx = null;
    this.buffers = new Map(); // trackId|bpm -> AudioBuffer
    this.sources = [];
    this.playing = false;
    this.startedAt = 0;   // ctx.currentTime when playback started
    this.startPos = 0;    // mix-time position where playback started
  }

  _ensureCtx() {
    if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    return this.ctx;
  }

  async load(trackId, bpm) {
    const key = `${trackId}|${bpm ?? "native"}`;
    if (this.buffers.has(key)) return this.buffers.get(key);
    const ctx = this._ensureCtx();
    const resp = await fetch(api.audioUrl(trackId, bpm));
    if (!resp.ok) throw new Error(`audio ${trackId}@${bpm}: ${resp.status}`);
    const buf = await ctx.decodeAudioData(await resp.arrayBuffer());
    this.buffers.set(key, buf);
    return buf;
  }

  // mix: state.js mix object; overlap: {start,end}|null; pos: mix-time seconds
  play(mix, overlap, pos = 0) {
    const ctx = this._ensureCtx();
    if (ctx.state === "suspended") ctx.resume();
    this.stop();
    const t0 = ctx.currentTime + 0.05;

    mix.tracks.forEach((track, idx) => {
      const key = `${track.id}|${track.bpm ?? "native"}`;
      const buf = this.buffers.get(key);
      if (!buf) return;
      const role = idx === 0 ? "out" : "in";
      const src = ctx.createBufferSource();
      src.buffer = buf;
      const gain = ctx.createGain();
      src.connect(gain).connect(ctx.destination);

      const trackStart = track.offset - pos; // seconds from now until track starts
      const offsetInto = Math.max(0, pos - track.offset);
      if (offsetInto >= buf.duration) return;

      // Base gain 1, crossfade over the overlap zone (if any).
      gain.gain.setValueAtTime(role === "in" && overlap && pos < overlap.start ? 0 : 1, t0);
      if (overlap && overlap.end > pos) {
        const fadeStart = Math.max(overlap.start, pos);
        const curve = gainCurve(fadeStart, overlap.end, role, 128);
        const when = t0 + Math.max(0, fadeStart - pos);
        gain.gain.setValueAtTime(curve[0], when);
        try {
          gain.gain.setValueCurveAtTime(new Float32Array(curve), when, overlap.end - fadeStart);
        } catch {
          curve.forEach((v, i) =>
            gain.gain.linearRampToValueAtTime(v, when + ((overlap.end - fadeStart) * i) / (curve.length - 1)));
        }
      }
      if (trackStart > 0) src.start(t0 + trackStart, 0);
      else src.start(t0, offsetInto);
      this.sources.push(src);
    });

    this.playing = true;
    this.startedAt = t0;
    this.startPos = pos;
  }

  position() {
    if (!this.playing || !this.ctx) return this.startPos;
    return this.startPos + Math.max(0, this.ctx.currentTime - this.startedAt);
  }

  stop() {
    this.sources.forEach((s) => { try { s.stop(); } catch { /* already stopped */ } });
    this.sources = [];
    const pos = this.playing ? this.position() : this.startPos;
    this.playing = false;
    this.startPos = pos;
  }

  seek(mix, overlap, pos) {
    const wasPlaying = this.playing;
    this.stop();
    this.startPos = pos;
    if (wasPlaying) this.play(mix, overlap, pos);
  }
}
