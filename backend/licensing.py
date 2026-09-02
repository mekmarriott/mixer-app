"""CC license handling (requirements.md §1, §2).

Every track stores its exact CC variant *and version*. ND tracks are
hard-excluded from derivative (time-stretched) rendering and from mixing
features; SA and NC are flagged for downstream export-license handling and
commercial gating.

Version matters: Jamendo's catalog is largely CC 3.0 (with some ported 2.x),
not 4.0. Recording a 3.0 track as "CC BY 4.0" would attribute it under a
licence it was never released under, so the version travels with the name and
`parse_license` refuses anything it cannot identify exactly (test P1-07) —
never blanking or defaulting.
"""
import re

# Canonical variant token -> (nd, sa, nc) flags.
CC_VARIANT_FLAGS = {
    "BY":          (False, False, False),
    "BY-SA":       (False, True, False),
    "BY-NC":       (False, False, True),
    "BY-ND":       (True, False, False),
    "BY-NC-SA":    (False, True, True),
    "BY-NC-ND":    (True, False, True),
}
# The versions Creative Commons actually published. Anything else is a typo or
# a licence we do not understand, and must be rejected rather than assumed.
CC_VERSIONS = ("1.0", "2.0", "2.5", "3.0", "4.0")

# "CC BY-NC-SA 3.0" or, for a ported licence, "CC BY-SA 2.0 UK".
_LICENSE_RE = re.compile(
    r"^CC (?P<variant>BY(?:-NC)?(?:-SA|-ND)?) "
    r"(?P<version>" + "|".join(v.replace(".", r"\.") for v in CC_VERSIONS) + r")"
    r"(?: (?P<jurisdiction>[A-Z]{2}))?$")

# Canonical unported names — what a well-formed catalog entry looks like.
KNOWN_VARIANTS = {f"CC {v} {ver}" for v in CC_VARIANT_FLAGS for ver in CC_VERSIONS}


def license_url(name):
    """Canonical creativecommons.org URL for a license name."""
    m = _LICENSE_RE.match(name or "")
    if not m:
        raise ValueError(f"Unknown or unsupported CC license: {name!r}")
    slug = m["variant"].lower()
    parts = [slug, m["version"]]
    if m["jurisdiction"]:
        parts.append(m["jurisdiction"].lower())
    return "https://creativecommons.org/licenses/" + "/".join(parts) + "/"


class _LicenseUrls:
    """Mapping-style access to canonical license URLs, for any valid name."""

    def __getitem__(self, name):
        return license_url(name)

    def __contains__(self, name):
        return _LICENSE_RE.match(name or "") is not None

    def __iter__(self):
        return iter(sorted(KNOWN_VARIANTS))


CC_URLS = _LicenseUrls()


def parse_license(name):
    """Return structured flags for a CC license string. Raises on unknown
    variants — a track must never be ingested with a blank/defaulted license
    (test P1-07)."""
    m = _LICENSE_RE.match(name or "")
    if not m:
        raise ValueError(f"Unknown or unsupported CC license: {name!r}")
    nd, sa, nc = CC_VARIANT_FLAGS[m["variant"]]
    return {
        "license": name,
        "license_url": license_url(name),
        "license_version": m["version"],
        "nd": nd,
        "sa": sa,
        "nc": nc,
    }


def attribution(track):
    """Attribution payload the UI must display (tests P4-26..28).

    `track` is any row carrying name/artist/license \u2014 a ``db.Track`` or the
    blob-free ``db.ListTrackSummariesRow``."""
    return {
        "text": f"“{track.name}” by {track.artist}",
        "artist": track.artist,
        "title": track.name,
        "license": track.license,
        "license_url": license_url(track.license),
    }
