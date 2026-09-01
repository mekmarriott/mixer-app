// Nav bar viewport math — pure, no DOM (tests P4-08..P4-10).
//
// The nav bar is a minimap of the whole mix; the viewport rectangle is the
// visible window of the timeline. Dragging the rect pans; dragging its edges
// resizes it, which zooms (ui-requirements §4).

export const MIN_VIEW_S = 4; // can't zoom tighter than 4 seconds

export function createViewport(totalDur) {
  return { start: 0, dur: Math.max(totalDur, MIN_VIEW_S), total: Math.max(totalDur, MIN_VIEW_S) };
}

export function setTotal(vp, totalDur) {
  vp.total = Math.max(totalDur, MIN_VIEW_S);
  return clamp(vp);
}

export function clamp(vp) {
  vp.dur = Math.min(Math.max(vp.dur, MIN_VIEW_S), vp.total);
  vp.start = Math.min(Math.max(vp.start, 0), vp.total - vp.dur);
  return vp;
}

// Pan: move viewport start by dt seconds (drag of the rect body).
export function pan(vp, dt) {
  vp.start += dt;
  return clamp(vp);
}

// Zoom by dragging an edge: 'left' moves start, 'right' moves end.
export function resizeEdge(vp, edge, dt) {
  if (edge === "left") {
    const end = vp.start + vp.dur;
    vp.start = Math.min(vp.start + dt, end - MIN_VIEW_S);
    vp.start = Math.max(vp.start, 0);
    vp.dur = end - vp.start;
  } else {
    vp.dur = vp.dur + dt;
  }
  return clamp(vp);
}

// Coordinate mapping between mix-time and pixels for a given canvas width.
export function timeToPx(vp, t, width) {
  return ((t - vp.start) / vp.dur) * width;
}
export function pxToTime(vp, px, width) {
  return vp.start + (px / width) * vp.dur;
}

// Minimap mapping: whole mix across the bar's width.
export function minimapTimeToPx(vp, t, width) {
  return (t / vp.total) * width;
}
export function minimapPxToTime(vp, px, width) {
  return (px / width) * vp.total;
}
