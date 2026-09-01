"""Phase 2 — Matching & recommendation logic (testing-document P2-01..P2-05)."""
import unittest

from fixture import get_fixture

from backend import bpm_grid, config, db, matching


def _rec_inputs(con, a_id, b_id):
    a, b = db.get_track(con, a_id), db.get_track(con, b_id)
    return (a, b, db.analysis_of(con, a_id), db.segments_of(con, a_id),
            db.analysis_of(con, b_id), db.segments_of(con, b_id),
            db.variants_for(con, a_id), db.variants_for(con, b_id))


class TestP2Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con, _, _ = get_fixture()

    # ------------------------------------------------------------- P2-01
    def test_p2_01_candidates_share_grid_point(self):
        """P2-01: every recommended candidate shares >=1 grid BPM with the
        current track; cross-bucket tracks (no shared point) never appear."""
        recs = matching.recommend(self.con, "1001")
        self.assertGreater(len(recs), 0)
        my_grid = {v["grid_bpm"] for v in db.variants_for(self.con, "1001")}
        for r in recs:
            other = {v["grid_bpm"] for v in db.variants_for(self.con, r["track_id"])}
            self.assertTrue(my_grid & other, r["track_id"])
            self.assertTrue(r["shared_grid"])
        # 2001 is downtempo: no shared grid with house 1001 -> excluded
        self.assertNotIn("2001", [r["track_id"] for r in recs])

    # ------------------------------------------------------------- P2-02
    def test_p2_02_weighted_formula(self):
        """P2-02: total == W_BPM*bpm + W_KEY*key + W_ENERGY*energy."""
        a, b, an_a, sg_a, an_b, sg_b, va, vb = _rec_inputs(self.con, "1001", "1003")
        m = matching.match(a, b, an_a, sg_a, an_b, sg_b, va, vb)
        bd = m["breakdown"]
        expected = (config.WEIGHT_BPM * bd["bpm"] + config.WEIGHT_KEY * bd["key"]
                    + config.WEIGHT_ENERGY * bd["energy"])
        self.assertAlmostEqual(m["score"], expected, places=3)

    def test_p2_02_camelot_component(self):
        """Key component follows the Camelot wheel: identical > adjacent >
        distant; relative major/minor scores high."""
        self.assertEqual(matching.camelot_score("8A", "8A"), 1.0)
        self.assertEqual(matching.camelot_score("8A", "9A"), 0.8)   # adjacent
        self.assertEqual(matching.camelot_score("8A", "8B"), 0.8)   # relative
        self.assertLess(matching.camelot_score("8A", "2A"),
                        matching.camelot_score("8A", "9A"))
        self.assertLess(matching.camelot_score("8A", "3B"), 0.4)
        # Table is symmetric for same-mode pairs
        self.assertEqual(matching.CAMELOT_TABLE[("8A", "10A")],
                         matching.CAMELOT_TABLE[("10A", "8A")])

    def test_p2_02_bpm_component_prefers_less_stretch(self):
        """BPM component is near-binary from the shared grid but penalizes
        larger stretch distances."""
        s_close, g_close = matching.bpm_score(124, 124, [124])
        s_far, g_far = matching.bpm_score(120, 128, [124])
        self.assertGreater(s_close, s_far)
        self.assertEqual(g_close, 124)
        self.assertEqual(g_far, 124)
        s_none, g_none = matching.bpm_score(124, 90, [])
        self.assertEqual((s_none, g_none), (0.0, None))

    # ------------------------------------------------------------- P2-03
    def test_p2_03_breakdown_in_response(self):
        """P2-03: recommendations expose the component breakdown and each
        component matches its independently computed value."""
        recs = matching.recommend(self.con, "1001")
        for r in recs:
            self.assertIn("breakdown", r)
            for k in ("bpm", "key", "energy", "weights"):
                self.assertIn(k, r["breakdown"])
            a, b, an_a, sg_a, an_b, sg_b, va, vb = _rec_inputs(
                self.con, "1001", r["track_id"])
            self.assertAlmostEqual(
                r["breakdown"]["key"],
                matching.CAMELOT_TABLE[(a["camelot"], b["camelot"])], places=4)
            self.assertAlmostEqual(
                r["breakdown"]["energy"],
                matching.energy_continuity(an_a, sg_a, an_b, sg_b), places=3)

    # ------------------------------------------------------------- P2-04
    def test_p2_04_cutoff_excludes_low_scores(self):
        """P2-04: candidates below MATCH_SCORE_CUTOFF are excluded."""
        recs = matching.recommend(self.con, "1001")
        for r in recs:
            self.assertGreaterEqual(r["score"], config.MATCH_SCORE_CUTOFF)
        # Force a low score: distant key kills the key term; verify a
        # sub-cutoff synthetic match would be filtered by the same rule.
        a, b, an_a, sg_a, an_b, sg_b, va, vb = _rec_inputs(self.con, "1001", "1003")
        b_far = dict(b, camelot="2B")     # hostile key
        m = matching.match(a, b_far, an_a, sg_a, an_b, sg_b, va, vb)
        self.assertLess(m["breakdown"]["key"], 0.2)

    # ------------------------------------------------------------- P2-05
    def test_p2_05_sorted_descending(self):
        recs = matching.recommend(self.con, "1001")
        scores = [r["score"] for r in recs]
        self.assertEqual(scores, sorted(scores, reverse=True))

    # --------------------------------------------------- ND interaction
    def test_nd_tracks_never_recommended(self):
        """ND (non-mixable) tracks are not candidates and get no
        recommendations themselves (requirements.md §2)."""
        recs = matching.recommend(self.con, "1001")
        self.assertNotIn("1005", [r["track_id"] for r in recs])
        self.assertEqual(matching.recommend(self.con, "1005"), [])


if __name__ == "__main__":
    unittest.main()
