// Thin API client. All heavy computation happened at ingestion; these calls
// only read cached results (ui-requirements: no server round-trips on drag).

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  tracks: () => getJSON("/api/tracks"),
  track: (id) => getJSON(`/api/tracks/${id}`),
  waveform: (id, bpm, points = 480) =>
    getJSON(`/api/tracks/${id}/waveform?points=${points}${bpm ? `&bpm=${bpm}` : ""}`),
  recommendations: (id) => getJSON(`/api/tracks/${id}/recommendations`),
  transitions: (a, b) => getJSON(`/api/transitions?a=${a}&b=${b}`),
  credits: () => getJSON("/api/credits"),
  audioUrl: (id, bpm) => `/api/tracks/${id}/audio${bpm ? `?bpm=${bpm}` : ""}`,
};
