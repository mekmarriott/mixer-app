// Attribution — pure, no DOM (tests P4-26..P4-28; requirements.md §1).
//
// Every track shown or played must display: artist, title, and a link to the
// track's specific CC license.

export function attributionLine(att) {
  if (!att) return "";
  return `\u201c${att.title}\u201d by ${att.artist} \u2014 ${att.license}`;
}

// Data for a rendered attribution row: text + the license link target.
export function attributionParts(att) {
  return {
    text: attributionLine(att),
    licenseName: att.license,
    licenseUrl: att.license_url,
  };
}

export function licenseBadges(flags) {
  const out = [];
  if (flags?.nd) out.push({ code: "ND", label: "No derivatives \u2014 playback only, cannot be mixed" });
  if (flags?.sa) out.push({ code: "SA", label: "ShareAlike \u2014 exported mixes must carry the same license" });
  if (flags?.nc) out.push({ code: "NC", label: "NonCommercial \u2014 not usable in a monetized deployment" });
  return out;
}
