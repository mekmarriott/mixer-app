"""Phase 3 — Transition-point detection (testing-document P3-01..P3-05)."""
import unittest

from fixture import get_fixture

from backend import db, transitions
from backend.analysis import rescale_analysis
from backend.segmentation import ENTRY_ROLES, EXIT_ROLES


def _pair(con, a_id, b_id, grid):
    a, b = db.get_track(con, a_id), db.get_track(con, b_id)
    an_a = rescale_analysis(db.analysis_of(con, a_id), grid / a["native_bpm"])
    an_b = rescale_analysis(db.analysis_of(con, b_id), grid / b["native_bpm"])
    return an_a, db.segments_of(con, a_id), an_b, db.segments_of(con, b_id)


class TestP3Transitions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con, _, _ = get_fixture()
        cls.an_a, cls.sg_a, cls.an_b, cls.sg_b = _pair(cls.con, "1001", "1003", 123)
        cls.result = transitions.score_pair(cls.an_a, cls.sg_a, cls.an_b, cls.sg_b)

    # ------------------------------------------------------------- P3-01
    def test_p3_01_regions_align_with_structure(self):
        """P3-01: exit candidate regions favor outro/breakdown structural
        zones (verified against the known synthetic section layout: outro is
        the final ~12%, breakdown mid-track)."""
        regions = transitions._candidate_regions(self.sg_a, EXIT_ROLES)
        labels = [r["label"] for r in regions]
        self.assertIn("outro", labels)
        self.assertTrue(set(labels) <= {"outro", "breakdown", "verse"},
                        f"high-energy zones leaked into exit regions: {labels}")
        entry = transitions._candidate_regions(self.sg_b, ENTRY_ROLES)
        self.assertIn("intro", [r["label"] for r in entry])

    # ------------------------------------------------------------- P3-02
    def test_p3_02_windows_beat_aligned(self):
        """P3-02: every candidate window start sits on the (downbeat) grid —
        no off-grid starts."""
        hop_a = self.an_a["frames"]["hop_dur"]
        downbeats_a = [b / hop_a for b in self.an_a["beat_grid"][::4]]
        for c in self.result["curve"]:
            frame = c["a_start_s"] / hop_a
            nearest = min(abs(frame - d) for d in downbeats_a)
            self.assertLessEqual(nearest, 1.0,
                                 f"off-grid window start at {c['a_start_s']}s")

    # ------------------------------------------------------------- P3-03
    def test_p3_03_scoring_reads_prefix_sums_only(self):
        """P3-03: window scoring never re-scans raw frame data. The frame
        arrays are replaced with len-only sentinels that raise on element
        access; scoring must still succeed because aggregates come from the
        prefix sums."""
        class LenOnly(list):
            def __getitem__(self, i):
                raise AssertionError("raw frame data was re-scanned")
            def __iter__(self):
                raise AssertionError("raw frame data was iterated")

        def guard(analysis):
            g = dict(analysis)
            g["frames"] = dict(analysis["frames"])
            g["frames"]["rms"] = LenOnly(analysis["frames"]["rms"])
            g["frames"]["flux"] = LenOnly(analysis["frames"]["flux"])
            g["frames"]["bass_ratio"] = LenOnly(analysis["frames"]["bass_ratio"])
            return g

        res = transitions.score_pair(guard(self.an_a), self.sg_a,
                                     guard(self.an_b), self.sg_b)
        self.assertGreater(len(res["curve"]), 0)

    # ------------------------------------------------------------- P3-04
    def test_p3_04_best_matches_known_good_fixture(self):
        """P3-04: the best window pair for the verified fixture pair
        (1001 -> 1003 @ 123 BPM) is a late exit from A into an early entry of
        B — the manually-verified 'good transition' shape for these synthetic
        tracks (A outro starts at ~88% of 60s)."""
        best = self.result["best"]
        dur_a = self.an_a["duration_s"]
        dur_b = self.an_b["duration_s"]
        self.assertGreater(best["a_start_s"], dur_a * 0.6, "exit not late in A")
        self.assertLess(best["b_start_s"], dur_b * 0.25, "entry not early in B")
        self.assertGreater(best["score"], 0.6)
        self.assertEqual(best["score"], max(c["score"] for c in self.result["curve"]))

    def test_p3_04_score_components_present_and_bounded(self):
        for c in self.result["curve"]:
            for k in ("energy", "phase", "spectral", "role"):
                self.assertIn(k, c["components"])
                self.assertGreaterEqual(c["components"][k], 0.0)
                self.assertLessEqual(c["components"][k], 1.0)
            self.assertGreaterEqual(c["score"], 0.0)
            self.assertLessEqual(c["score"], 1.0)

    # ------------------------------------------------------------- P3-05
    def test_p3_05_full_curve_retrievable(self):
        """P3-05: the full scored curve is returned (not just the top hit),
        with enough resolution for marker rendering."""
        self.assertGreater(len(self.result["curve"]), len(self.result["markers"]))
        self.assertGreaterEqual(len(self.result["markers"]), 2)
        # Markers are distinct A-positions drawn from the same curve.
        positions = [m["a_start_s"] for m in self.result["markers"]]
        self.assertEqual(len(positions), len(set(positions)))
        curve_keys = {(c["a_start_s"], c["b_start_s"]) for c in self.result["curve"]}
        for m in self.result["markers"]:
            self.assertIn((m["a_start_s"], m["b_start_s"]), curve_keys)


if __name__ == "__main__":
    unittest.main()
