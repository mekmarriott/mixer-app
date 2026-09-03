"""Phase 3 — Transition-point detection (testing-document P3-01..P3-05)."""
import unittest

from fixture import get_fixture, read

from backend import config, transitions
from backend.analysis import rescale_analysis
from backend.segmentation import ENTRY_ROLES, EXIT_ROLES


def _pair(a_id, b_id, grid):
    with read() as q:
        a, b = q.get_track(id=a_id), q.get_track(id=b_id)
    from backend import analysis_store
    an_a = rescale_analysis(analysis_store.hydrate(a.id, a.analysis_json),
                            grid / a.native_bpm)
    an_b = rescale_analysis(analysis_store.hydrate(b.id, b.analysis_json),
                            grid / b.native_bpm)
    return an_a, a.segments_json, an_b, b.segments_json


class TestP3Transitions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database, _, _ = get_fixture()
        cls.an_a, cls.sg_a, cls.an_b, cls.sg_b = _pair("1001", "1003", 123)
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


class TestFadeLength(unittest.TestCase):
    """A transition's LENGTH, which is not the same as the overlap it sits in."""

    def test_fade_is_graded_from_blend_quality(self):
        good = {"spectral": 1.0, "energy": 1.0, "phase": 1.0}
        poor = {"spectral": 0.0, "energy": 0.0, "phase": 0.0}
        self.assertEqual(transitions.fade_bars(good), config.FADE_BARS_LADDER[-1])
        self.assertEqual(transitions.fade_bars(poor), config.FADE_BARS_LADDER[0])

    def test_every_rung_is_a_musical_length_in_range(self):
        """The whole ladder lands where a transition is actually heard.

        The fade used to span the overlap, which the marker search places for
        alignment rather than for length, so it could run for minutes.
        """
        for bpm in (100, 125, 150):
            for bars in config.FADE_BARS_LADDER:
                seconds = bars * 4 * (60.0 / bpm)
                self.assertGreaterEqual(seconds, 3.0)
                self.assertLessEqual(seconds, 40.0)

    def test_room_steps_the_fade_down_the_ladder(self):
        """A fade is stepped to a shorter rung, not clipped to fit.

        Clipping would leave it on an arbitrary number of beats; the rung below
        is still a whole number of bars.
        """
        cand = {"components": {"spectral": 1.0, "energy": 1.0, "phase": 1.0}}
        roomy = transitions.fade_for(cand, 120, room_s=1000)
        tight = transitions.fade_for(cand, 120, room_s=4.0)
        self.assertEqual(roomy["fade_bars"], config.FADE_BARS_LADDER[-1])
        self.assertIn(tight["fade_bars"], config.FADE_BARS_LADDER)
        self.assertLess(tight["fade_bars"], roomy["fade_bars"])
        self.assertAlmostEqual(
            tight["fade_s"], tight["fade_bars"] * 4 * (60.0 / 120), places=3)

    def test_no_room_still_yields_the_shortest_rung(self):
        cand = {"components": {"spectral": 1.0, "energy": 1.0, "phase": 1.0}}
        none = transitions.fade_for(cand, 120, room_s=0.0)
        self.assertEqual(none["fade_bars"], config.FADE_BARS_LADDER[0])


class TestScoringTermsDiscriminate(unittest.TestCase):
    """A term that cannot vary cannot choose, whatever weight it carries."""

    def test_harmonic_separates_agreement_from_disagreement(self):
        """Chroma is compared by SHAPE, not angle.

        These vectors are non-negative, sum-normalised and log-compressed, so
        every one is near uniform and the cosine between any two is close to 1
        — measured across 5124 real windows it ran 0.961..0.999. Centring
        first leaves the pattern, which is the part that says which pitch
        classes dominate.
        """
        same = [0.30, 0.05, 0.02, 0.20, 0.03, 0.10, 0.02, 0.18, 0.02, 0.04, 0.02, 0.02]
        # The same energy, moved onto different pitch classes.
        other = [0.02, 0.30, 0.20, 0.02, 0.18, 0.02, 0.10, 0.02, 0.05, 0.03, 0.04, 0.02]
        agree = matching_similarity(same, same)
        disagree = matching_similarity(same, other)
        self.assertAlmostEqual(agree, 1.0, places=6)
        self.assertLess(disagree, 0.5)
        self.assertGreater(agree - disagree, 0.5,
                           "harmonic term must separate these by a wide margin")

    def test_a_flat_window_scores_nothing_rather_than_everything(self):
        flat = [1 / 12] * 12
        peaky = [0.5] + [0.5 / 11] * 11
        self.assertIsNone(matching_similarity(flat, peaky))


def matching_similarity(a, b):
    from backend.analysis import chroma_similarity
    return chroma_similarity(a, b)
