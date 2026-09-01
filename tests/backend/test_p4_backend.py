"""Phase 4 — backend-side API tests (testing-document P4-01, P4-11,
P4-26..P4-29) plus the transition-API compliance guards. Pure-UI P4 items are
covered by the frontend suite (tests/frontend/) and the manual QA list in
docs/design-document.md."""
import unittest

from fixture import get_fixture

from backend import db
from backend.app import create_app


class TestP4Backend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con, _, cls.tmp = get_fixture()
        app = create_app(run_ingestion=False)
        app.config["TESTING"] = True
        cls.client = app.test_client()

    # ------------------------------------------------------------- P4-01
    def test_p4_01_audio_serves_prerendered_variant(self):
        """P4-01: the audio endpoint streams the pre-rendered variant file
        byte-for-byte — no runtime stretch happens at request time."""
        vpath = self.tmp / "variants" / "1001_123.wav"
        self.assertTrue(vpath.exists())
        on_disk = vpath.read_bytes()
        r = self.client.get("/api/tracks/1001/audio?bpm=123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, on_disk)

    def test_p4_01_missing_variant_404(self):
        r = self.client.get("/api/tracks/1001/audio?bpm=200")
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------- P4-11
    def test_p4_11_waveform_from_cached_analysis(self):
        """P4-11: waveform envelope is served from cached analysis (values
        are a normalized downsample of the stored RMS frames)."""
        r = self.client.get("/api/tracks/1001/waveform?points=50")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(len(d["points"]), 50)
        a = db.analysis_of(self.con, "1001")
        peak = max(a["frames"]["rms"])
        # First and last envelope points correspond to first/last rms frames.
        self.assertAlmostEqual(d["points"][0], a["frames"]["rms"][0] / peak, places=2)
        self.assertAlmostEqual(max(d["points"]), 1.0, places=3)
        self.assertAlmostEqual(d["duration_s"], a["duration_s"], places=3)

    def test_p4_11_waveform_bpm_rescale(self):
        """Waveform at a grid BPM rescales duration/beat grid by the ratio."""
        a = db.analysis_of(self.con, "1001")
        r = self.client.get("/api/tracks/1001/waveform?points=50&bpm=123").get_json()
        ratio = 123 / a["bpm"]
        self.assertAlmostEqual(r["duration_s"], a["duration_s"] / ratio, places=3)
        self.assertAlmostEqual(r["beat_grid"][10], a["beat_grid"][10] / ratio, places=3)

    # ---------------------------------------------------- P4-26 .. P4-28
    def test_p4_26_27_attribution_for_all_tracks(self):
        """P4-26/27: every catalog entry (either mix slot) carries artist,
        title, and the specific CC license link."""
        tracks = self.client.get("/api/tracks").get_json()
        self.assertGreaterEqual(len(tracks), 5)
        for t in tracks:
            att = t["attribution"]
            self.assertEqual(att["artist"], t["artist"])
            self.assertEqual(att["title"], t["name"])
            self.assertIn("creativecommons.org/licenses/", att["license_url"])
            self.assertIn(att["title"], att["text"])
            self.assertIn(att["artist"], att["text"])

    def test_p4_28_attribution_matches_stored_variant(self):
        """P4-28: attribution reflects each track's stored CC variant —
        spot-checked across BY, BY-NC, and BY-SA."""
        tracks = {t["id"]: t for t in self.client.get("/api/tracks").get_json()}
        expect = {
            "1001": ("CC BY 4.0", "licenses/by/4.0"),
            "1003": ("CC BY-NC 4.0", "licenses/by-nc/4.0"),
            "1002": ("CC BY-SA 4.0", "licenses/by-sa/4.0"),
        }
        for tid, (name, url_part) in expect.items():
            att = tracks[tid]["attribution"]
            self.assertEqual(att["license"], name)
            self.assertIn(url_part, att["license_url"])

    # ------------------------------------------------------------- P4-29
    def test_p4_29_credits_endpoint(self):
        """P4-29: credits payload lists Essentia (AGPL), Rubber Band (GPL),
        wavesurfer.js (BSD-3-Clause), Tone.js (MIT), each with a link."""
        credits = {c["name"]: c for c in self.client.get("/api/credits").get_json()}
        self.assertIn("AGPL", credits["Essentia"]["license"])
        self.assertIn("GPL", credits["Rubber Band Library"]["license"])
        self.assertIn("BSD", credits["wavesurfer.js"]["license"])
        self.assertIn("MIT", credits["Tone.js"]["license"])
        for c in credits.values():
            self.assertTrue(c["url"].startswith("http"))

    # ------------------------------------- transition API compliance guards
    def test_transitions_nd_pair_forbidden(self):
        """ND tracks cannot enter the mixing flow (requirements.md §2):
        the transitions API refuses the pair outright."""
        r = self.client.get("/api/transitions?a=1001&b=1005")
        self.assertEqual(r.status_code, 403)

    def test_transitions_no_shared_grid_conflict(self):
        r = self.client.get("/api/transitions?a=1001&b=2001")
        self.assertEqual(r.status_code, 409)

    def test_transitions_happy_path_payload(self):
        r = self.client.get("/api/transitions?a=1001&b=1003")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for key in ("grid_bpm", "curve", "markers", "best", "match", "window_s"):
            self.assertIn(key, d)
        self.assertEqual(d["match"]["best_grid_bpm"], d["grid_bpm"])

    # ---------------------------------------------- catalog flags (PL-10 aid)
    def test_license_flags_exposed_for_commercial_audit(self):
        """The /api/tracks payload exposes nd/sa/nc flags so the PL-10
        catalog audit is a pure query, not a re-ingestion."""
        tracks = self.client.get("/api/tracks").get_json()
        flagged = {t["id"]: t["license_flags"] for t in tracks}
        self.assertTrue(flagged["1005"]["nd"])
        self.assertTrue(flagged["1002"]["sa"])
        self.assertTrue(flagged["1003"]["nc"])
        self.assertFalse(any(flagged["1001"].values()))


if __name__ == "__main__":
    unittest.main()
