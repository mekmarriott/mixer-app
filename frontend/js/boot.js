// Warmup / readiness presentation — pure logic, no DOM.
//
// The server binds its port before the catalog exists, so the client must be
// able to describe "not ready yet" precisely rather than hanging or rendering
// an empty app. These helpers turn a /api/status payload into what the boot
// overlay shows.

export const PHASES = {
  ingesting: "Ingesting catalog",
  precomputing: "Precomputing waveforms",
  ready: "Ready",
  failed: "Startup failed",
};

export function isReady(status) {
  return Boolean(status && status.ready === true && status.phase === "ready");
}

export function isFailed(status) {
  return Boolean(status && status.phase === "failed");
}

/** 0..100, clamped. Falls back to the done/total pair when percent is absent. */
export function progressPercent(status) {
  if (!status) return 0;
  if (typeof status.percent === "number") {
    return Math.max(0, Math.min(100, Math.round(status.percent)));
  }
  if (status.total > 0) {
    return Math.max(0, Math.min(100, Math.round((100 * status.done) / status.total)));
  }
  return 0;
}

/** Headline line for the overlay. */
export function statusMessage(status) {
  if (!status) return "Connecting to the server…";
  if (isFailed(status)) return status.error || "Startup failed";
  return status.message || PHASES[status.phase] || "Working…";
}

/** Secondary line: counts and elapsed time, omitted when meaningless. */
export function statusDetail(status) {
  if (!status) return "";
  if (isFailed(status)) return "Check the server log, then reload.";
  const bits = [];
  if (status.total > 0) bits.push(`${status.done}/${status.total}`);
  if (typeof status.elapsed_s === "number") bits.push(`${status.elapsed_s}s`);
  return bits.join(" · ");
}

/**
 * Poll backoff: fast while a short warmup might already be done, easing off so
 * a long ingest is not hammered. Bounded, so the overlay always stays live.
 */
export function pollDelayMs(attempt, { start = 250, max = 2000 } = {}) {
  return Math.min(max, Math.round(start * Math.pow(1.4, Math.max(0, attempt))));
}
