// Web Audio playback engine (P4-01, P4-02, P4-25).
//
// Every track is a pre-rendered variant at the SAME grid BPM — no live
// time-stretch happens here (project plan Phase 4). The crossfade schedules
// gain values sampled from crossfade.js, the same module the timeline uses
// to draw the fade, so sight and sound cannot drift apart.
//
// A mix is a chain: each track fades in across the overlap with its
// predecessor and out across the overlap with its successor, so an interior
// track carries a composed envelope and at most two tracks ever sound at once.

import { gainCurve, trackGainAt } from "./crossfade.js";
import { offsets, overlapsFor } from "./state.js";
import { api } from "./api.js";

// A few-hour mix must not schedule hundreds of buffers at once, and a browser
// will not hold them in memory anyway. Only tracks that sound within this
// horizon of the play position are scheduled; `refresh()` extends it as
// playback advances.
const SCHEDULE_HORIZON_S = 90;

export class Player {
  constructor() {
    this.ctx = null;
    this.buffers = new Map(); // trackId|bpm -> AudioBuffer
    this.sources = [];
    this.playing = false;
    this.startedAt = 0;   // ctx.currentTime when playback started
    this.startPos = 0;    // mix-time position where playback started
    this.scheduled = new Set();   // chain indices already handed to WebAudio
    this.scheduledThrough = 0;
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

  // mix: state.js mix object; pos: mix-time seconds.
  //
  // Every track in the chain is scheduled with its OWN composed gain envelope:
  // an interior track fades in across the overlap with its predecessor and out
  // across the overlap with its successor. Sampling the composed curve (rather
  // than scheduling each zone separately) is what keeps a short track that is
  // fading in and out at once correct.
  play(mix, pos = 0) {
    const ctx = this._ensureCtx();
    if (ctx.state === "suspended") ctx.resume();
    this.stop();
    const t0 = ctx.currentTime + 0.05;
    this.playing = true;
    this.startedAt = t0;
    this.startPos = pos;
    this.scheduled = new Set();
    this.scheduledThrough = pos;
    this._extend(mix, pos + SCHEDULE_HORIZON_S);
  }

  /**
   * Schedule every not-yet-scheduled track that begins before `through`.
   *
   * Additive on purpose: already-playing sources are never stopped and
   * restarted, because re-scheduling from scratch mid-playback would put an
   * audible seam in the mix every time the horizon advanced.
   */
  _extend(mix, through) {
    const ctx = this.ctx;
    const offs = offsets(mix);
    const pos = this.startPos;

    mix.tracks.forEach((track, idx) => {
      if (this.scheduled.has(idx)) return;
      const start = offs[idx];
      const end = start + track.duration;
      if (end <= pos || start > through) return;

      const key = `${track.id}|${track.bpm ?? "native"}`;
      const buf = this.buffers.get(key);
      if (!buf) return;                       // not loaded yet; a later pass takes it

      const offsetInto = Math.max(0, pos - start);
      if (offsetInto >= buf.duration) return;

      const src = ctx.createBufferSource();
      src.buffer = buf;
      const gain = ctx.createGain();
      src.connect(gain).connect(ctx.destination);

      const overlaps = overlapsFor(mix, idx);
      const from = Math.max(start, pos);
      const when = this.startedAt + (from - pos);

      gain.gain.setValueAtTime(trackGainAt(from, overlaps), when);
      if (end > from) {
        const curve = gainCurve(from, end, overlaps, 256);
        try {
          gain.gain.setValueCurveAtTime(new Float32Array(curve), when, end - from);
        } catch {
          curve.forEach((v, i) =>
            gain.gain.linearRampToValueAtTime(v, when + ((end - from) * i) / (curve.length - 1)));
        }
      }

      if (start > pos) src.start(this.startedAt + (start - pos), 0);
      else src.start(this.startedAt, offsetInto);
      this.sources.push(src);
      this.scheduled.add(idx);
    });

    this.scheduledThrough = Math.max(this.scheduledThrough, through);
  }

  /**
   * Extend the scheduling horizon as the play head advances. Called from the
   * animation loop; a no-op until the head is within half a horizon of the end
   * of what is already scheduled. Adds sources without disturbing playing ones.
   */
  refresh(mix) {
    if (!this.playing) return false;
    const pos = this.position();
    if (pos < this.scheduledThrough - SCHEDULE_HORIZON_S / 2) return false;
    this._extend(mix, pos + SCHEDULE_HORIZON_S);
    return true;
  }

  position() {
    if (!this.playing || !this.ctx) return this.startPos;
    return this.startPos + Math.max(0, this.ctx.currentTime - this.startedAt);
  }

  stop() {
    this.sources.forEach((s) => { try { s.stop(); } catch { /* already stopped */ } });
    this.sources = [];
    this.scheduled = new Set();
    const pos = this.playing ? this.position() : this.startPos;
    this.playing = false;
    this.startPos = pos;
  }

  seek(mix, pos) {
    const wasPlaying = this.playing;
    this.stop();
    this.startPos = pos;
    if (wasPlaying) this.play(mix, pos);
  }
}
