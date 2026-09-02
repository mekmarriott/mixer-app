"""Bulk catalog discovery (backend/discover.py) and tempo-band resolution.

No network: the API is a stub, so the filters and the paging logic are tested
against listings that are shaped like Jamendo's but built here.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import bpm_grid, config, discover, jamendo     # noqa: E402
from backend.bpm_grid import AUTO                           # noqa: E402

CC_BY = "http://creativecommons.org/licenses/by/3.0/"
CC_BY_ND = "http://creativecommons.org/licenses/by-nc-nd/3.0/"


def row(tid, ccurl=CC_BY, allowed=True, **kw):
    base = {"id": tid, "name": "T%s" % tid, "artist_name": "A%s" % tid,
            "license_ccurl": ccurl, "audiodownload_allowed": allowed,
            "duration": 200, "shareurl": "https://jamendo.test/%s" % tid}
    base.update(kw)
    return base


def fake_api(pages):
    """A stub `get` serving successive pages, recording the params it saw."""
    calls = []

    def get(url, params, timeout=30):
        calls.append(params)
        idx = params["offset"] // discover.PAGE
        return {"results": pages[idx] if idx < len(pages) else []}

    get.calls = calls
    return get


class NoLimit:
    def acquire(self):
        pass


def _nosleep(_seconds):
    """The empty-page retry backoff is production behaviour; the unit suite
    should not spend seven seconds per exhausted listing waiting it out."""


class TestUsableEntry(unittest.TestCase):
    def test_plain_by_track_is_kept_and_marked_auto(self):
        e = discover.usable_entry(row("1"))
        self.assertEqual(e["id"], "1")
        self.assertEqual(e["license"], "CC BY 3.0")
        self.assertEqual(e["genre"], AUTO)

    def test_nd_licence_is_rejected(self):
        """ND forbids derivatives, and every variant is one — the pipeline
        would refuse it before downloading anyway."""
        self.assertIsNone(discover.usable_entry(row("1", ccurl=CC_BY_ND)))

    def test_undownloadable_track_is_rejected(self):
        self.assertIsNone(discover.usable_entry(row("1", allowed=False)))

    def test_unparseable_licence_is_skipped_not_guessed(self):
        self.assertIsNone(discover.usable_entry(row("1", ccurl="")))
        self.assertIsNone(
            discover.usable_entry(row("1", ccurl="https://example.com/nope")))

    def test_excluded_id_is_rejected(self):
        self.assertIsNone(discover.usable_entry(row("1"), exclude={"1"}))


class TestEmptyPageRetry(unittest.TestCase):
    """Jamendo returns empty-but-successful pages at a high rate; treating one
    as the end of the listing silently truncates the import."""

    def test_a_spurious_empty_page_is_retried_not_treated_as_the_end(self):
        seq = [[], [], [row("1"), row("2")]]
        calls = []

        def get(url, params, timeout=30):
            calls.append(params)
            return {"results": seq[len(calls) - 1] if len(calls) <= len(seq) else []}

        found, _ = discover.discover(2, get=get, limiter=NoLimit(),
                                     sleep=lambda _s: None, client_id="x")
        self.assertEqual([e["id"] for e in found], ["1", "2"])
        self.assertEqual(len(calls), 3)

    def test_a_genuinely_empty_listing_ends_after_the_retries(self):
        calls = []

        def get(url, params, timeout=30):
            calls.append(params)
            return {"results": []}

        found, _ = discover.discover(10, get=get, limiter=NoLimit(),
                                     sleep=lambda _s: None, client_id="x")
        self.assertEqual(found, [])
        self.assertEqual(len(calls), jamendo.EMPTY_RESULT_RETRIES + 1)

    def test_retry_does_not_advance_the_offset(self):
        """A retry must re-ask for the SAME page, or the gap it was covering
        is skipped."""
        seq = [[], [row("1")]]
        calls = []

        def get(url, params, timeout=30):
            calls.append(dict(params))
            return {"results": seq[len(calls) - 1] if len(calls) <= len(seq) else []}

        discover.discover(1, get=get, limiter=NoLimit(),
                          sleep=lambda _s: None, client_id="x")
        self.assertEqual([c["offset"] for c in calls], [0, 0])


class TestDiscover(unittest.TestCase):
    def test_pages_until_it_has_enough(self):
        pages = [[row(str(i)) for i in range(200)],
                 [row(str(i)) for i in range(200, 400)]]
        get = fake_api(pages)
        found, stats = discover.discover(250, get=get, limiter=NoLimit(),
                                         sleep=_nosleep, client_id="x")
        self.assertEqual(len(found), 250)
        self.assertEqual(len(get.calls), 2)
        self.assertEqual([c["offset"] for c in get.calls], [0, 200])

    def test_stops_when_the_listing_runs_out(self):
        get = fake_api([[row("1"), row("2")]])
        found, stats = discover.discover(100, get=get, limiter=NoLimit(),
                                         sleep=_nosleep, client_id="x")
        self.assertEqual(len(found), 2)

    def test_filtered_rows_do_not_count_toward_the_target(self):
        """Asking for 2 usable tracks must not return 2 rows of which one is
        ND — the point of filtering here is that the count means something."""
        page = [row("1", ccurl=CC_BY_ND), row("2"), row("3", allowed=False),
                row("4")]
        get = fake_api([page])
        found, stats = discover.discover(2, get=get, limiter=NoLimit(),
                                         sleep=_nosleep, client_id="x")
        self.assertEqual([e["id"] for e in found], ["2", "4"])
        self.assertEqual(stats["rejected"], 2)

    def test_duplicates_within_a_listing_are_dropped(self):
        get = fake_api([[row("1"), row("1"), row("2")]])
        found, _ = discover.discover(10, get=get, limiter=NoLimit(),
                                     sleep=_nosleep, client_id="x")
        self.assertEqual([e["id"] for e in found], ["1", "2"])

    def test_already_held_ids_are_excluded(self):
        get = fake_api([[row("1"), row("2"), row("3")]])
        found, _ = discover.discover(10, get=get, limiter=NoLimit(),
                                     sleep=_nosleep, client_id="x", exclude={"2"})
        self.assertEqual([e["id"] for e in found], ["1", "3"])

    def test_download_filter_uses_the_numeric_true_the_api_expects(self):
        """`audiodownload_allowed=true` returns an empty page with
        `status: success` — a silent filter that looks like an empty tag."""
        get = fake_api([[row("1")]])
        discover.discover(1, get=get, limiter=NoLimit(), sleep=_nosleep,
                          client_id="x")
        self.assertEqual(get.calls[0]["audiodownload_allowed"], "1")

    def test_tag_is_passed_through_and_omitted_when_absent(self):
        get = fake_api([[row("1")]])
        discover.discover(1, tag="electronic", get=get, limiter=NoLimit(),
                          sleep=_nosleep, client_id="x")
        self.assertEqual(get.calls[0]["tags"], "electronic")
        get2 = fake_api([[row("1")]])
        discover.discover(1, get=get2, limiter=NoLimit(), sleep=_nosleep,
                          client_id="x")
        self.assertNotIn("tags", get2.calls[0])

    def test_written_catalog_is_shaped_like_the_shipped_one(self):
        get = fake_api([[row("1"), row("2")]])
        found, _ = discover.discover(2, get=get, limiter=NoLimit(),
                                     sleep=_nosleep, client_id="x")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tracks.json"
            discover.write_catalog(path, found, "https://jamendo.test/page")
            doc = json.loads(path.read_text())
        shipped = json.loads(
            (Path(__file__).resolve().parents[2] / "config" / "tracks.json").read_text())
        self.assertEqual(doc["mode"], shipped["mode"])
        self.assertEqual(set(doc), set(shipped))
        # Every field the publisher reads off an entry must be present. The
        # shipped file also carries bpm/key measured at curation time, which
        # discovery cannot know; it adds source_url, which the shipped file
        # keeps only at the top level.
        required = {"id", "name", "artist", "genre", "license"}
        self.assertLessEqual(required, set(doc["tracks"][0]))


class TestResolveBucket(unittest.TestCase):
    def test_auto_derives_the_band_from_the_analysed_bpm(self):
        self.assertEqual(bpm_grid.resolve_bucket(AUTO, 124.0), "house")
        self.assertEqual(bpm_grid.resolve_bucket(AUTO, 78.0), "slow")
        self.assertEqual(bpm_grid.resolve_bucket(None, 168.0), "fast")
        self.assertEqual(bpm_grid.resolve_bucket("", 92.0), "downtempo")

    def test_a_curated_band_is_kept(self):
        self.assertEqual(bpm_grid.resolve_bucket("downtempo", 124.0),
                         "downtempo")

    def test_bpm_outside_every_band_yields_no_grid(self):
        self.assertIsNone(bpm_grid.resolve_bucket(AUTO, 20.0))
        self.assertEqual(bpm_grid.grid_points(20.0, None), [])

    def test_deriving_beats_defaulting_to_one_band(self):
        """The regression this exists to prevent: a fixed band silently
        renders no variants for anything outside it."""
        for bpm in (78, 92, 105, 140, 168):
            self.assertEqual(bpm_grid.grid_points(bpm, "house"), [])
            band = bpm_grid.resolve_bucket(AUTO, bpm)
            self.assertTrue(bpm_grid.grid_points(bpm, band),
                            "no grid for %s BPM in band %r" % (bpm, band))

    def test_every_band_is_reachable_from_its_own_midpoint(self):
        for name, (lo, hi) in config.BPM_BANDS.items():
            mid = (lo + hi) / 2.0
            self.assertEqual(bpm_grid.resolve_bucket(AUTO, mid), name)


if __name__ == "__main__":
    unittest.main()
