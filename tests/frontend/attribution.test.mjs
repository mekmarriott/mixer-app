// Attribution display content (testing-document P4-26..P4-28).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  attributionLine, attributionParts, licenseBadges,
} from "../../frontend/js/attribution.js";

const att = (license, url) => ({
  title: "Neon Corridor", artist: "Volt Array", license, license_url: url,
});

test("P4-26: attribution line contains title, artist, and license name", () => {
  const line = attributionLine(att("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"));
  assert.match(line, /Neon Corridor/);
  assert.match(line, /Volt Array/);
  assert.match(line, /CC BY 4\.0/);
});

test("P4-26/27: parts expose the specific license link for rendering (both slots use the same builder)", () => {
  const p = attributionParts(att("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"));
  assert.equal(p.licenseName, "CC BY-SA 4.0");
  assert.equal(p.licenseUrl, "https://creativecommons.org/licenses/by-sa/4.0/");
  assert.match(p.text, /by Volt Array/);
});

test("P4-28: attribution reflects the stored CC variant across BY / BY-NC / BY-SA", () => {
  const cases = [
    ["CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"],
    ["CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"],
    ["CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"],
  ];
  for (const [name, url] of cases) {
    const p = attributionParts(att(name, url));
    assert.ok(p.text.endsWith(name), `${name} not in line`);
    assert.equal(p.licenseUrl, url);
  }
});

test("empty attribution renders nothing rather than a broken line", () => {
  assert.equal(attributionLine(null), "");
});

test("license badges: ND / SA / NC each produce a distinct explanatory badge", () => {
  assert.deepEqual(licenseBadges({}), []);
  const nd = licenseBadges({ nd: true });
  assert.equal(nd.length, 1);
  assert.equal(nd[0].code, "ND");
  assert.match(nd[0].label, /playback only/);
  const all = licenseBadges({ nd: true, sa: true, nc: true });
  assert.deepEqual(all.map((b) => b.code), ["ND", "SA", "NC"]);
  assert.match(all[1].label, /same license/);
  assert.match(all[2].label, /monetized/i);
});
