// Thin API client. All heavy computation happened at ingestion; these calls
// only read cached results (ui-requirements: no server round-trips on drag).

async function sendJSON(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${method} ${url}: ${r.status} ${detail}`);
  }
  return r.status === 204 ? null : r.json();
}

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

  // Saved mixes. The chain is the only thing persisted: ordering plus one gap
  // per track — which is why a drag can afford to write on every gesture.
  mixes: () => getJSON("/api/mixes"),
  mix: (id) => getJSON(`/api/mixes/${id}`),
  createMix: (name) => sendJSON("POST", "/api/mixes", { name }),
  renameMix: (id, name) => sendJSON("PATCH", `/api/mixes/${id}`, { name }),
  deleteMix: (id) => sendJSON("DELETE", `/api/mixes/${id}`),
  putMixTracks: (id, tracks) => sendJSON("PUT", `/api/mixes/${id}/tracks`, { tracks }),
  moveMixTrack: (id, node, deltaBeats) =>
    sendJSON("PATCH", `/api/mixes/${id}/tracks/${node}`, { delta_beats: deltaBeats }),
  audioUrl: (id, bpm) => `/api/tracks/${id}/audio${bpm ? `?bpm=${bpm}` : ""}`,
};
