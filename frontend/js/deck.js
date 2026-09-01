// Suggested-deck helpers — pure, no DOM (tests P4-12..P4-14).

// Recommendations arrive ranked from the API (P2-05); keep a defensive sort.
export function rankRecommendations(recs) {
  return [...(recs || [])].sort((a, b) => b.score - a.score);
}

export function scorePercent(score) {
  return Math.round(Math.max(0, Math.min(1, score)) * 100);
}

// Pie indicator geometry: SVG arc path filling `score` of the circle,
// starting at 12 o'clock (P4-13: pie fill matches the percentage).
export function pieAngleDeg(score) {
  return Math.max(0, Math.min(1, score)) * 360;
}

export function piePath(score, r = 8, cx = 9, cy = 9) {
  const ang = pieAngleDeg(score);
  if (ang <= 0) return "";
  if (ang >= 360) {
    return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.01} ${cy - r} Z`;
  }
  const rad = ((ang - 90) * Math.PI) / 180;
  const x = cx + r * Math.cos(rad);
  const y = cy + r * Math.sin(rad);
  const large = ang > 180 ? 1 : 0;
  return `M ${cx} ${cy} L ${cx} ${cy - r} A ${r} ${r} 0 ${large} 1 ${x} ${y} Z`;
}
