// Mix state — pure logic, no DOM. Enforces the v1 rule: at most two tracks,
// track 2 overlaps track 1's tail (tests P4-17..P4-19).

export const MAX_TRACKS = 2;

export function createMix() {
  return { title: "Untitled Mix", tracks: [] };
}

export function canAddTrack(mix) {
  return mix.tracks.length < MAX_TRACKS;
}

// track: {id, name, artist, duration, offset, color} — offset = seconds on
// the shared mix timeline where this track starts.
export function addTrack(mix, track) {
  if (!canAddTrack(mix)) {
    return { ok: false, reason: "Two tracks are already loaded. Remove one to add another." };
  }
  const t = { ...track, offset: track.offset ?? 0 };
  mix.tracks.push(t);
  return { ok: true, track: t };
}

export function removeTrack(mix, id) {
  const i = mix.tracks.findIndex((t) => t.id === id);
  if (i >= 0) mix.tracks.splice(i, 1);
  return i >= 0;
}

export function setOffset(mix, id, offset) {
  const t = mix.tracks.find((x) => x.id === id);
  if (!t) return false;
  t.offset = Math.max(0, offset);
  return true;
}

export function totalDuration(mix) {
  return mix.tracks.reduce((m, t) => Math.max(m, t.offset + t.duration), 0);
}

// Overlap zone between the two tracks (null when <2 tracks or no overlap).
export function overlapZone(mix) {
  if (mix.tracks.length < 2) return null;
  const [a, b] = mix.tracks;
  const start = Math.max(a.offset, b.offset);
  const end = Math.min(a.offset + a.duration, b.offset + b.duration);
  return end > start ? { start, end } : null;
}

export function formatTime(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
