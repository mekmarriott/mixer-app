// Thin API client. All heavy computation happened at ingestion; these calls
// only read cached results (ui-requirements: no server round-trips on drag).

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) {
    // 503 means the catalog is still warming; carry the status payload so the
    // caller can show progress instead of a generic failure.
    if (r.status === 503) {
      const body = await r.json().catch(() => ({}));
      const err = new Error(`${url}: warming up`);
      err.warmingUp = true;
      err.status = body.status || null;
      throw err;
    }
    throw new Error(`${url}: ${r.status} ${await r.text()}`);
  }
  return r.json();
}

export const api = {
  status: () => getJSON("/api/status"),
  deck: () => getJSON("/api/deck"),
  tracks: () => getJSON("/api/tracks"),
  track: (id) => getJSON(`/api/tracks/${id}`),
  waveform: (id, bpm, points = 480) =>
    getJSON(`/api/tracks/${id}/waveform?points=${points}${bpm ? `&bpm=${bpm}` : ""}`),
  recommendations: (id) => getJSON(`/api/tracks/${id}/recommendations`),
  transitions: (a, b) => getJSON(`/api/transitions?a=${a}&b=${b}`),
  credits: () => getJSON("/api/credits"),
  audioUrl: (id, bpm) => `/api/tracks/${id}/audio${bpm ? `?bpm=${bpm}` : ""}`,
};
