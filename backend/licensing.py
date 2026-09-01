"""CC license handling (requirements.md §1, §2).

Every track stores its exact CC variant. ND tracks are hard-excluded from
derivative (time-stretched) rendering and from mixing features; SA and NC are
flagged for downstream export-license handling and commercial gating."""

CC_URLS = {
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-NC 4.0": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-ND 4.0": "https://creativecommons.org/licenses/by-nd/4.0/",
    "CC BY-NC-SA 4.0": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "CC BY-NC-ND 4.0": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
}
KNOWN_VARIANTS = set(CC_URLS)


def parse_license(name):
    """Return structured flags for a CC license string. Raises on unknown
    variants — a track must never be ingested with a blank/defaulted license
    (test P1-07)."""
    if name not in KNOWN_VARIANTS:
        raise ValueError(f"Unknown or unsupported CC license: {name!r}")
    return {
        "license": name,
        "license_url": CC_URLS[name],
        "nd": "ND" in name,
        "sa": "SA" in name,
        "nc": "NC" in name,
    }


def attribution(track_row):
    """Attribution payload the UI must display (tests P4-26..28)."""
    return {
        "text": f"\u201c{track_row['name']}\u201d by {track_row['artist']}",
        "artist": track_row["artist"],
        "title": track_row["name"],
        "license": track_row["license"],
        "license_url": CC_URLS[track_row["license"]],
    }
