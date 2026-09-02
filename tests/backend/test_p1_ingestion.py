"""Phase 1 — Ingestion & analysis pipeline (testing-document P1-01..P1-10)."""
import unittest
from unittest import mock

import numpy as np

from fixture import FIXTURE_TRACKS, get_fixture, read, spec

from backend import analysis, bpm_grid, config, ingest, jamendo, licensing


class TestP1Ingestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database, cls.results, cls.tmp = get_fixture()

    # ------------------------------------------------------------- P1-01
    def test_p1_01_download_gate_accepts_allowed(self):
        """P1-01: pipeline only accepts tracks with audiodownload_allowed."""
        meta = {"id": "x", "audiodownload_allowed": True}
        self.assertIs(jamendo.validate_source_meta(meta), meta)

    def test_p1_01_download_gate_rejects_disallowed(self):
        with self.assertRaises(jamendo.TrackSourceError):
            jamendo.validate_source_meta({"id": "x", "audiodownload_allowed": False})
        with self.assertRaises(jamendo.TrackSourceError):
            jamendo.validate_source_meta({"id": "x"})  # missing flag = rejected

    # ------------------------------------------------------------- P1-02
    def test_p1_02_cached_bpm_key_beatgrid(self):
        """P1-02: every ANALYSED track has cached BPM, key, beat grid.

        Scoped to mixable tracks: an ND licence forbids the variants that
        analysis exists to produce, so those tracks are recorded from their
        metadata and never downloaded or analysed (LIC-01). Asserting analysis
        for them would be asserting work we deliberately do not do.
        """
        with read() as q:
            tracks = [t for t in q.list_tracks() if t.mixable]
        self.assertTrue(tracks)
        for t in tracks:
            a = t.analysis_json
            self.assertIsNotNone(a, t.id)
            self.assertGreater(a["bpm"], 0)
            self.assertIn("camelot", a["key"])
            self.assertGreater(len(a["beat_grid"]), 10)
            # Accuracy against the known synthetic ground truth:
            s = spec(t.id)
            self.assertLess(abs(a["bpm"] - s["bpm"]), 2.0,
                            f"{t.id}: bpm {a['bpm']} vs {s['bpm']}")
            self.assertEqual(a["key"]["camelot"], s["key"], t.id)

    def test_p1_02_beat_grid_spacing_matches_bpm(self):
        with read() as q:
            a = self.database.full_analysis("1001")
        diffs = np.diff(a["beat_grid"])
        self.assertAlmostEqual(float(np.median(diffs)), 60.0 / a["bpm"], delta=0.02)

    # ------------------------------------------------------------- P1-03
    def test_p1_03_segmentation_labels(self):
        """P1-03: labeled sections exist for every ANALYSED track (see P1-02
        on why unmixable tracks are excluded)."""
        allowed = {"intro", "verse", "build", "drop", "breakdown", "outro", "full"}
        with read() as q:
            tracks = [t for t in q.list_tracks() if t.mixable]
        self.assertTrue(tracks)
        for t in tracks:
            segs = t.segments_json
            self.assertGreaterEqual(len(segs), 3, t.id)
            self.assertEqual(segs[0]["label"], "intro")
            self.assertEqual(segs[-1]["label"], "outro")
            for s in segs:
                self.assertIn(s["label"], allowed)
            # contiguous, non-overlapping
            for s1, s2 in zip(segs[:-1], segs[1:]):
                self.assertEqual(s1["end_frame"], s2["start_frame"])

    # ------------------------------------------------------------- P1-04
    def test_p1_04_prefix_sums_match_bruteforce(self):
        """P1-04: prefix-sum window aggregates == brute-force sums."""
        with read() as q:
            a = self.database.full_analysis("1001")
        rms = a["frames"]["rms"]
        prefix = a["prefix"]["rms"]
        rng = np.random.default_rng(7)
        for _ in range(25):
            i, j = sorted(rng.integers(0, len(rms), 2))
            if i == j:
                j += 1
            brute = sum(rms[i:j]) / (j - i)
            fast = analysis.window_mean(prefix, i, j)
            self.assertAlmostEqual(fast, brute, places=9)

    def test_p1_04_window_mean_is_o1(self):
        """window_mean touches only two prefix entries regardless of window
        size — verified structurally with an access-counting sequence."""
        class Counting(list):
            def __init__(self, data):
                super().__init__(data)
                self.reads = 0
            def __getitem__(self, i):
                self.reads += 1
                return super().__getitem__(i)

        with read() as q:
            a = self.database.full_analysis("1001")
        p = Counting(a["prefix"]["rms"])
        analysis.window_mean(p, 0, len(p) - 1)      # widest window
        wide_reads = p.reads
        p.reads = 0
        analysis.window_mean(p, 5, 9)               # tiny window
        self.assertEqual(wide_reads, p.reads)       # identical access count
        self.assertLessEqual(wide_reads, 2)

    # ------------------------------------------------------------- P1-05
    def test_p1_05_variants_for_every_grid_point_in_tolerance(self):
        """P1-05: every mixable track has a variant at each in-tolerance
        grid point of its genre bucket."""
        with read() as q:
            tracks = q.list_tracks()
            variants = {t.id: q.list_variants_for_track(track_id=t.id)
                        for t in tracks}
        for t in tracks:
            if not t.mixable:
                continue
            expected = bpm_grid.grid_points(t.native_bpm, t.genre)
            self.assertEqual([v.grid_bpm for v in variants[t.id]], expected, t.id)
            for v in variants[t.id]:
                self.assertLessEqual(abs(v.ratio - 1.0), config.MAX_STRETCH_RATIO + 1e-9)
                self.assertTrue(
                    (self.tmp / "variants"
                     / f"{t.id}_{v.grid_bpm}.{config.delivery_ext()}").exists())

    # ------------------------------------------------------------- P1-06
    def test_p1_06_rescale_matches_ratio_no_reanalysis(self):
        """P1-06: variant analysis comes from rescaling, not re-analysis."""
        with read() as q:
            a = self.database.full_analysis("1001")
        ratio = 1.05
        r = analysis.rescale_analysis(a, ratio)
        self.assertAlmostEqual(r["bpm"], a["bpm"] * ratio, places=2)
        self.assertAlmostEqual(r["duration_s"], a["duration_s"] / ratio, places=4)
        for orig, scaled in zip(a["beat_grid"][:20], r["beat_grid"][:20]):
            self.assertAlmostEqual(scaled, orig / ratio, places=4)
        self.assertTrue(r["rescaled_from_native"])

    def test_p1_06_analyze_called_once_per_track(self):
        """Ingesting a track runs the full analysis pass exactly once even
        though many variants are rendered."""
        entry = {"id": "9901", "name": "Probe", "artist": "T", "genre": "house",
                 "bpm": 124, "key": "8A", "license": "CC BY 4.0", "duration_s": 20}
        # Keep the shared fixture catalog pristine. Variants go with the track:
        # the schema cascades the delete.
        self.addCleanup(self.database.catalog.forget_track, "9901")

        with mock.patch("backend.ingest.analysis_mod.analyze",
                        side_effect=analysis.analyze) as spy:
            ingest.ingest_track(self.database, entry, "offline")
        self.assertEqual(spy.call_count, 1)
        with read() as q:
            self.assertGreater(len(q.list_variants_for_track(track_id="9901")), 1)

    def test_p1_06_deleting_a_track_cascades_to_its_variants(self):
        """The FK cascade is what keeps the addCleanup above honest."""
        entry = {"id": "9902", "name": "Probe2", "artist": "T", "genre": "house",
                 "bpm": 124, "key": "8A", "license": "CC BY 4.0", "duration_s": 20}
        ingest.ingest_track(self.database, entry, "offline")
        with read() as q:
            self.assertGreater(len(q.list_variants_for_track(track_id="9902")), 1)
        self.database.catalog.forget_track("9902")
        with read() as q:
            self.assertIsNone(q.get_track(id="9902"))
            self.assertEqual(q.list_variants_for_track(track_id="9902"), [])

    # ------------------------------------------------------------- P1-07
    def test_p1_07_specific_license_stored(self):
        """P1-07: every track stores its exact CC variant; unknown licenses
        are rejected at ingestion, never blanked or defaulted."""
        with read() as q:
            tracks = q.list_tracks()
        for t in tracks:
            self.assertIn(t.license, licensing.KNOWN_VARIANTS, t.id)
            self.assertEqual(t.license, spec(t.id)["license"])
        with self.assertRaises(ValueError):
            licensing.parse_license("CC0")
        with self.assertRaises(ValueError):
            licensing.parse_license("")

    # ------------------------------------------------------------- P1-08
    def test_p1_08_nd_excluded_from_variants(self):
        """P1-08: zero stretched-variant files exist for ND tracks."""
        with read() as q:
            nd = [t for t in q.list_tracks() if t.license_nd]
            self.assertGreaterEqual(len(nd), 1)
            for t in nd:
                self.assertFalse(t.mixable)
                self.assertEqual(q.list_variants_for_track(track_id=t.id), [])
                files = list((self.tmp / "variants").glob(f"{t.id}_*.wav"))
                self.assertEqual(files, [], f"ND track {t.id} has variant files")

    # ------------------------------------------------------- P1-09 / P1-10
    def test_p1_09_sa_flagged(self):
        with read() as q:
            sa = {t.id for t in q.list_tracks() if t.license_sa}
        self.assertEqual(sa, {t["id"] for t in FIXTURE_TRACKS if "SA" in t["license"]})

    def test_p1_10_nc_flagged(self):
        with read() as q:
            nc = {t.id for t in q.list_tracks() if t.license_nc}
        self.assertEqual(nc, {t["id"] for t in FIXTURE_TRACKS if "NC" in t["license"]})

    # ------------------------------------------------------- exit criteria
    def test_p1_exit_criteria(self):
        """Every non-ND track has complete cached analysis + full variant set."""
        with read() as q:
            for t in q.list_tracks():
                if t.license_nd:
                    continue
                self.assertIsNotNone(t.analysis_json)
                self.assertIsNotNone(t.segments_json)
                expected = bpm_grid.grid_points(t.native_bpm, t.genre)
                got = [v.grid_bpm for v in q.list_variants_for_track(track_id=t.id)]
                self.assertEqual(got, expected)


class TestJamendoEmptyResultRetry(unittest.TestCase):
    """P1-01 (source layer): Jamendo answers valid queries with an empty
    HTTP 200 intermittently — measured at 27-50% on the live API, and
    all-or-nothing rather than partial. An empty success must be retried, never
    reported as a missing track, or an import silently drops tracks and still
    reports success.

    The transport and the sleep are injected, so the retry policy is tested
    without network and without real delays."""

    OK = {"headers": {"code": 0}, "results": [{"id": "1001", "name": "Neon"}]}
    EMPTY = {"headers": {"code": 0, "error_message": ""}, "results": []}

    def _get(self, *payloads):
        """A fake transport returning `payloads` in order, recording calls."""
        calls = []

        def get(url, params, timeout=30):
            calls.append((url, dict(params)))
            return payloads[min(len(calls) - 1, len(payloads) - 1)]

        return get, calls

    def setUp(self):
        self.slept = []

    def test_empty_success_is_retried_until_results_arrive(self):
        get, calls = self._get(self.EMPTY, self.EMPTY, self.OK)
        track = jamendo.fetch_track_metadata(
            "1001", "cid", get=get, sleep=self.slept.append)
        self.assertEqual(track["id"], "1001")
        self.assertEqual(len(calls), 3)

    def test_a_first_attempt_hit_does_not_sleep(self):
        get, calls = self._get(self.OK)
        jamendo.fetch_track_metadata("1001", "cid", get=get, sleep=self.slept.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_backoff_grows_between_attempts(self):
        get, _ = self._get(self.EMPTY, self.EMPTY, self.OK)
        jamendo.fetch_track_metadata("1001", "cid", get=get, sleep=self.slept.append)
        self.assertEqual(self.slept, [jamendo.RETRY_BASE_DELAY_S,
                                      jamendo.RETRY_BASE_DELAY_S * 2])

    def test_persistent_empty_does_not_claim_the_track_is_missing(self):
        """The whole point: exhausting retries reports what happened rather
        than asserting absence."""
        get, calls = self._get(self.EMPTY)
        with self.assertRaises(jamendo.TrackSourceError) as cm:
            jamendo.fetch_track_metadata("1001", "cid", get=get,
                                         sleep=self.slept.append)
        message = str(cm.exception)
        self.assertIn("not proof the track is gone", message)
        self.assertNotIn("not found", message)
        self.assertEqual(len(calls), jamendo.EMPTY_RESULT_RETRIES + 1)

    def test_an_api_error_code_fails_immediately_without_retrying(self):
        """A bad client id or malformed query does not improve on retry."""
        error = {"headers": {"code": 5, "error_message": "Your credential is not valid"},
                 "results": []}
        get, calls = self._get(error)
        with self.assertRaises(jamendo.TrackSourceError) as cm:
            jamendo.fetch_track_metadata("1001", "cid", get=get,
                                         sleep=self.slept.append)
        self.assertIn("credential is not valid", str(cm.exception))
        self.assertEqual(len(calls), 1, "retried a permanent error")
        self.assertEqual(self.slept, [])

    def test_the_track_id_reaches_the_query(self):
        get, calls = self._get(self.OK)
        jamendo.fetch_track_metadata("2001", "cid", get=get, sleep=self.slept.append)
        self.assertEqual(calls[0][1]["id"], "2001")
        self.assertEqual(calls[0][1]["client_id"], "cid")


if __name__ == "__main__":
    unittest.main()
