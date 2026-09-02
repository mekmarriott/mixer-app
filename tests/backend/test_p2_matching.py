"""Phase 2 — Matching & recommendation logic (testing-document P2-01..P2-05)."""
import dataclasses
import unittest

from fixture import get_fixture, read

from backend import config, matching
from backend.db.catalog import grid_bpms_by_track


def _rec_inputs(q, a_id, b_id):
    """(track_a, track_b, analysis_a, segments_a, analysis_b, segments_b,
    grid_a, grid_b) — the argument tuple matching.match() takes."""
    a, b = q.get_track(id=a_id), q.get_track(id=b_id)
    grids = grid_bpms_by_track(q)
    return (a, b, a.analysis_json, a.segments_json,
            b.analysis_json, b.segments_json,
            grids.get(a_id, []), grids.get(b_id, []))


class TestP2Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database, _, _ = get_fixture()

    # ------------------------------------------------------------- P2-01
    def test_p2_01_candidates_share_grid_point(self):
        """P2-01: every recommended candidate shares >=1 grid BPM with the
        current track; cross-bucket tracks (no shared point) never appear."""
        with read() as q:
            recs = matching.recommend(q, "1001")
            grids = grid_bpms_by_track(q)
        self.assertGreater(len(recs), 0)
        my_grid = set(grids["1001"])
        for r in recs:
            self.assertTrue(my_grid & set(grids[r["track_id"]]), r["track_id"])
            self.assertTrue(r["shared_grid"])
        # 2001 is downtempo: no shared grid with house 1001 -> excluded
        self.assertNotIn("2001", [r["track_id"] for r in recs])

    # ------------------------------------------------------------- P2-02
    def test_p2_02_weighted_formula(self):
        """P2-02: total == W_BPM*bpm + W_KEY*key + W_ENERGY*energy."""
        with read() as q:
            m = matching.match(*_rec_inputs(q, "1001", "1003"))
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
        with read() as q:
            recs = matching.recommend(q, "1001")
            for r in recs:
                self.assertIn("breakdown", r)
                for k in ("bpm", "key", "energy", "weights"):
                    self.assertIn(k, r["breakdown"])
                a, b, an_a, sg_a, an_b, sg_b, _, _ = _rec_inputs(
                    q, "1001", r["track_id"])
                self.assertAlmostEqual(
                    r["breakdown"]["key"],
                    matching.CAMELOT_TABLE[(a.camelot, b.camelot)], places=4)
                self.assertAlmostEqual(
                    r["breakdown"]["energy"],
                    matching.energy_continuity(an_a, sg_a, an_b, sg_b), places=3)

    # ------------------------------------------------------------- P2-04
    def test_p2_04_cutoff_excludes_low_scores(self):
        """P2-04: candidates below MATCH_SCORE_CUTOFF are excluded."""
        with read() as q:
            recs = matching.recommend(q, "1001")
            for r in recs:
                self.assertGreaterEqual(r["score"], config.MATCH_SCORE_CUTOFF)
            # Force a low score: distant key kills the key term; verify a
            # sub-cutoff synthetic match would be filtered by the same rule.
            a, b, an_a, sg_a, an_b, sg_b, ga, gb = _rec_inputs(q, "1001", "1003")
        b_far = dataclasses.replace(b, camelot="2B")     # hostile key
        m = matching.match(a, b_far, an_a, sg_a, an_b, sg_b, ga, gb)
        self.assertLess(m["breakdown"]["key"], 0.2)

    # -------------------------------------------------------- result cap
    def test_recommendations_are_capped_and_keep_the_best(self):
        """The cap truncates after the sort, so it drops the weakest
        candidates and never changes which ones rank highest. Each result
        carries a waveform out and a multi-megabyte audition behind it, so the
        tail of a long list is paid for and never looked at."""
        with read() as q:
            everything = matching.recommend(q, "1001", limit=0)
            capped = matching.recommend(q, "1001", limit=2)
            default = matching.recommend(q, "1001")
        self.assertLessEqual(len(capped), 2)
        self.assertEqual([r["track_id"] for r in capped],
                         [r["track_id"] for r in everything[:2]])
        self.assertLessEqual(len(default), config.RECOMMENDATION_LIMIT)

    def test_a_cap_of_zero_means_no_limit(self):
        """0 is 'all of them', not 'none' — the escape hatch for callers that
        want the whole ranking, such as the cap's own test."""
        with read() as q:
            self.assertEqual(len(matching.recommend(q, "1001", limit=0)),
                             len(matching.recommend(q, "1001", limit=10_000)))

    # ------------------------------------------------------------- P2-05
    def test_p2_05_sorted_descending(self):
        with read() as q:
            recs = matching.recommend(q, "1001")
        scores = [r["score"] for r in recs]
        self.assertEqual(scores, sorted(scores, reverse=True))

    # --------------------------------------------------- ND interaction
    def test_region_energies_are_stored_at_ingest(self):
        """Scoring reads the energies off the row, so ingest must write them.

        Left NULL, matching falls back to reading analysis_json and
        segments_json per candidate — the whole-catalog blob read these
        columns exist to avoid.
        """
        with self.database.reading() as q:
            for row in q.list_track_summaries():
                if not row.mixable:
                    continue
                analysis = q.get_track_analysis(id=row.id)
                segments = q.get_track_segments(id=row.id)
                if not analysis or not segments:
                    continue
                outro, intro = matching.region_energies(analysis, segments)
                self.assertIsNotNone(row.outro_energy, row.id)
                self.assertIsNotNone(row.intro_energy, row.id)
                self.assertAlmostEqual(row.outro_energy, outro, places=6)
                self.assertAlmostEqual(row.intro_energy, intro, places=6)

    def test_scoring_falls_back_when_energies_are_missing(self):
        """A row predating the columns still scores, from its blobs.

        The fallback is what keeps an un-backfilled catalog correct rather
        than merely fast, so it has to produce the same answer.
        """
        with self.database.reading() as q:
            before = matching.recommend(q, "1001")
        with self.database.writing() as q:
            for row in q.list_track_summaries():
                q.set_track_energies(id=row.id, outro_energy=None,
                                     intro_energy=None)
        try:
            with self.database.reading() as q:
                after = matching.recommend(q, "1001")
            self.assertEqual([m["track_id"] for m in before],
                             [m["track_id"] for m in after])
            for b, a in zip(before, after):
                self.assertAlmostEqual(b["score"], a["score"], places=6)
        finally:
            with self.database.writing() as q:
                for row in q.list_track_summaries():
                    an = q.get_track_analysis(id=row.id)
                    sg = q.get_track_segments(id=row.id)
                    if not an or not sg:
                        continue
                    o, i = matching.region_energies(an, sg)
                    q.set_track_energies(id=row.id, outro_energy=o,
                                         intro_energy=i)

    def test_nd_tracks_never_recommended(self):
        """ND (non-mixable) tracks are not candidates and get no
        recommendations themselves (requirements.md §2)."""
        with read() as q:
            recs = matching.recommend(q, "1001")
            self.assertNotIn("1005", [r["track_id"] for r in recs])
            self.assertEqual(matching.recommend(q, "1005"), [])


if __name__ == "__main__":
    unittest.main()
