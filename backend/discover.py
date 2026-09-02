"""Find tracks on Jamendo and write a catalog file the publisher can ingest.

`config/tracks.json` was curated by hand: someone picked a playlist, ingested
it, and wrote the measured results back into the file. That does not scale to
"give me the top 1000 of this community", and the community pages themselves
are no help — they are client-rendered infinite scroll, so there is no list in
the HTML to read. The same listings come out of the public API, which is what
this module asks.

    python -m backend.discover --tag electronic --count 200 --out config/x.json
    python -m backend.discover --count 1000 --out config/y.json      # all genres

## What gets filtered out, and why here

Two filters run before a track reaches the catalog file, both to avoid paying
for something that would be discarded later:

* **`audiodownload_allowed` false** — the hard gate in
  `jamendo.validate_source_meta`. The pipeline could not use the audio.
* **ND licences** — `ingest._reject_unmixable_license` refuses these before
  downloading, because every tempo-matched variant is a derivative work and ND
  forbids derivatives. On this catalog that is roughly half of everything, so
  filtering at discovery is the difference between "top 200 tracks" and "top
  200 tracks you can actually mix".

A licence string the project cannot parse is skipped rather than guessed at:
`licensing.parse_license` refuses to default, and a track whose terms are not
understood must not be ingested under an assumed licence.

## Why entries carry `genre: auto`

`genre` in a catalog entry is a *tempo band*, not a musical genre, and it
selects the BPM grid a track's variants are rendered on. The API exposes no
usable tempo, so discovery cannot fill it in; `bpm_grid.resolve_bucket` derives
it from the analysed BPM instead. Writing a fixed band here would be worse than
leaving it blank — `grid_points` answers an out-of-band request with an empty
list rather than an error, so every track outside that one band would be
downloaded, analysed, stored, and silently left with no variants.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

from . import config, jamendo, licensing, ratelimit
from .bpm_grid import AUTO

API_URL = "https://api.jamendo.com/v3.0/tracks/"

#: What the community pages call "Trending". The other orders the API accepts
#: (popularity_total, popularity_month, buzzrate, releasedate) are passed
#: through unchanged by --order.
DEFAULT_ORDER = "popularity_week"

#: Results per listing page. This is the `limit` cap (200), NOT the much
#: smaller cap on multi-value parameters that governs id batching — a listing
#: query sends no id list. See jamendo.MAX_RESULTS_PER_REQUEST.
PAGE = 200

#: The API's boolean filters take 1/0, NOT true/false. Sending "true" returns
#: an EMPTY result set with `status: success` and no error message — a silent
#: filter that reads as "the listing is empty" rather than as a bad request.
#: Discovering nothing looks identical to a tag having no tracks, so this is
#: pinned to a constant and asserted in the tests.
TRUE = "1"

#: Give up rather than page forever when a listing is thinner than asked for,
#: or when the filters reject nearly everything.
MAX_PAGES = 200


class DiscoveryError(RuntimeError):
    pass


def _page(get, params, limiter, sleep, retries=None):
    """One listing page, retrying the empty-but-successful response.

    Jamendo answers a valid query with HTTP 200, `status: success` and an empty
    `results` array at a measured 27-50% rate (see jamendo.EMPTY_RESULT_RETRIES,
    which does the same for metadata lookups). Paging makes that worse than it
    is for a single lookup: treating the first empty page as the end of the
    listing silently truncates the import, and the run then *reports success*
    with a short catalog. Observed directly on this API — offset 1200 returned
    nothing while 1400 and 3000 returned full pages.

    Returns [] only after the retries are spent, which is the one case that
    really does mean "no more rows".
    """
    retries = jamendo.EMPTY_RESULT_RETRIES if retries is None else retries
    for attempt in range(retries + 1):
        limiter.acquire()
        rows = (get(API_URL, params) or {}).get("results") or []
        if rows:
            return rows
        if attempt < retries:
            sleep(jamendo.RETRY_BASE_DELAY_S * (2 ** attempt))
    return []


def _get_json(url, params, timeout=30):            # pragma: no cover - network
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        return json.load(resp)


def usable_entry(track, exclude=()):
    """A catalog entry for `track`, or None when it must not be ingested."""
    tid = str(track.get("id", ""))
    if not tid or tid in exclude:
        return None
    if not track.get("audiodownload_allowed"):
        return None
    try:
        name = jamendo._license_from_ccurl(track.get("license_ccurl", ""))
        lic = licensing.parse_license(name)
    except Exception:
        return None                                # unparseable: never guess
    if lic["nd"]:
        return None                                # refused before download anyway
    return {
        "id": tid,
        "name": track.get("name", ""),
        "artist": track.get("artist_name", ""),
        "genre": AUTO,                             # derived from the analysed BPM
        "license": name,
        "duration_s": float(track.get("duration") or 0.0),
        "source_url": track.get("shareurl", ""),
    }


def discover(count, tag=None, order=DEFAULT_ORDER, client_id=None, exclude=(),
             get=None, limiter=None, sleep=time.sleep, progress=None):
    """Up to `count` ingestible entries from the listing, best first.

    Pages until it has enough, the listing runs out, or MAX_PAGES is reached.
    `exclude` is a set of track ids already in the catalog, so asking for 200
    means 200 *new* tracks rather than 200 minus whatever is already held.
    """
    client_id = client_id or jamendo.require_client_id()
    get = get or _get_json
    limiter = limiter or ratelimit.TokenBucket(jamendo.DEFAULT_API_RATE)
    exclude = set(exclude)

    found, seen, offset, pages, rejected = [], set(), 0, 0, 0
    while len(found) < count and pages < MAX_PAGES:
        params = {"client_id": client_id, "format": "json", "limit": PAGE,
                  "offset": offset, "order": order,
                  "audiodownload_allowed": TRUE}
        if tag:
            params["tags"] = tag
        rows = _page(get, params, limiter, sleep)
        pages += 1
        offset += PAGE
        if not rows:
            break
        for row in rows:
            entry = usable_entry(row, exclude=exclude | seen)
            if entry is None:
                rejected += 1
                continue
            seen.add(entry["id"])
            found.append(entry)
            if len(found) >= count:
                break
        if progress:
            progress("  scanned %d, kept %d/%d" % (offset, len(found), count))
    return found, {"scanned": offset, "rejected": rejected, "pages": pages}


def write_catalog(path, entries, source, mode="jamendo"):
    """Write entries in the shape `config/tracks.json` uses."""
    doc = {
        "mode": mode,
        "source_playlist": source,
        "comment": (
            "Generated by backend/discover.py. `genre` is 'auto': it is a "
            "tempo band, derived from the analysed BPM at ingest time by "
            "bpm_grid.resolve_bucket, because the API exposes no usable "
            "tempo. ND-licensed and non-downloadable tracks are already "
            "filtered out, so every entry here is expected to ingest and "
            "render variants."),
        "tracks": entries,
    }
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def existing_ids():
    """Track ids already in the configured catalog, so they are not re-pulled."""
    from .db import Database
    database = Database.from_config()
    try:
        with database.reading() as q:
            return {t.id for t in q.list_tracks()}
    finally:
        database.dispose()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m backend.discover",
        description="Find ingestible Jamendo tracks and write a catalog file.")
    ap.add_argument("--count", type=int, required=True,
                    help="how many ingestible tracks to collect")
    ap.add_argument("--tag", default=None,
                    help="community tag, e.g. 'electronic'. Omit for all genres.")
    ap.add_argument("--order", default=DEFAULT_ORDER,
                    help="API ordering (default %s, what the site calls "
                         "Trending)" % DEFAULT_ORDER)
    ap.add_argument("--out", required=True, help="catalog file to write")
    ap.add_argument("--source", default=None,
                    help="the page this stands in for, recorded in the file")
    ap.add_argument("--include-existing", action="store_true",
                    help="do not exclude tracks already in the catalog")
    ap.add_argument("--exclude-file", action="append", default=[],
                    metavar="CATALOG",
                    help="also exclude every id in this catalog file; repeat "
                         "to chain several runs without overlap")
    args = ap.parse_args(argv)

    exclude = set() if args.include_existing else existing_ids()
    for path in args.exclude_file:
        with open(path) as fh:
            exclude |= {str(t["id"]) for t in json.load(fh).get("tracks", [])}
    print("collecting %d track(s)%s, order=%s%s"
          % (args.count, " tagged %r" % args.tag if args.tag else "",
             args.order,
             "" if args.include_existing else
             ", excluding %d already in the catalog" % len(exclude)))
    try:
        entries, stats = discover(args.count, tag=args.tag, order=args.order,
                                  exclude=exclude, progress=print)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    source = args.source or (
        "https://www.jamendo.com/community/%s/tracks" % (args.tag or "all-genres"))
    write_catalog(args.out, entries, source)
    print("scanned %d listing rows over %d request(s); %d rejected "
          "(ND licence, not downloadable, unparseable licence, or duplicate)"
          % (stats["scanned"], stats["pages"], stats["rejected"]))
    print("wrote %d entries to %s" % (len(entries), args.out))
    if len(entries) < args.count:
        print("NOTE: listing yielded fewer than requested", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
