"""Provider-seam tests: the real Essentia / Rubber Band / Jamendo dependencies.

These guard the seams that used to be stubs. The point is not just "does it
work" but "is the production engine actually the one running" — the previous
failure mode was analysis reporting `engine: essentia` while numpy/scipy did
all the work.

Engine-specific tests skip when the dependency is absent, so the suite still
passes on a bare install. The live Jamendo test additionally requires
DJMIXER_LIVE_TESTS=1 — it hits the network, and Jamendo's API is
intermittently lossy (see backend/jamendo.py).
"""
import os
import unittest

import numpy as np

from fixture import get_fixture, read

from backend import (analysis, audio_io, bpm_grid, config, jamendo,
                     licensing, stretch, synth)

LIVE = os.environ.get("DJMIXER_LIVE_TESTS", "").lower() in ("1", "true", "yes")


@unittest.skipUnless(analysis.HAVE_ESSENTIA, "essentia not installed")
class TestEssentiaEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database, cls.results, cls.tmp = get_fixture()

    def test_essentia_is_the_engine_that_actually_ran(self):
        """The stored analysis must carry the essentia path's own fields, not
        merely a label. engine_version/analysis_sr/bpm_confidence are produced
        only by _analyze_essentia."""
        with read() as q:
            tracks = q.list_tracks()
        for t in tracks:
            a = t.analysis_json
            self.assertEqual(a["engine"], "essentia", t.id)
            self.assertIn("engine_version", a)
            self.assertEqual(a["analysis_sr"], analysis.ANALYSIS_SR)
            self.assertIsInstance(a["bpm_confidence"], float)

    def test_engine_name_reports_the_live_engine(self):
        self.assertEqual(analysis.engine_name(), "essentia")

    def test_analysis_resamples_to_essentia_rate(self):
        """Regression: Essentia's rhythm extractors assume 44.1 kHz and expose
        no sampleRate parameter. Handing them our 22.05 kHz masters directly
        makes them read the audio as half-length and double-tempo, which put
        every detected BPM an octave out. Analysing a known 124 BPM signal at
        the native rate must still yield ~124, not ~248 (or its in-range fold)."""
        x = synth.synthesize({"id": "1001", "bpm": 124, "key": "8A", "duration_s": 30})
        a = analysis.analyze(x, config.SAMPLE_RATE)
        self.assertAlmostEqual(a["bpm"], 124.0, delta=2.0)
        # Beats must span the whole track, not the first half of it.
        self.assertGreater(a["beat_grid"][-1], 0.9 * a["duration_s"])

    def test_beat_grid_comes_from_tracked_beats(self):
        """Essentia returns real tracked beat times; spacing should match the
        reported tempo without being a synthesised uniform comb."""
        with read() as q:
            a = q.get_track_analysis(id="1001")
        diffs = np.diff(a["beat_grid"])
        self.assertAlmostEqual(float(np.median(diffs)), 60.0 / a["bpm"], delta=0.02)
        self.assertGreater(float(np.std(diffs)), 0.0, "beat grid is perfectly uniform")

    def test_key_maps_into_camelot(self):
        with read() as q:
            tracks = q.list_tracks()
        for t in tracks:
            self.assertIn(t.analysis_json["key"]["camelot"],
                          set(analysis.CAMELOT.values()), t.id)


@unittest.skipUnless(stretch.RUBBERBAND, "rubberband CLI not on PATH")
class TestRubberBandEngine(unittest.TestCase):
    def test_engine_name_reports_the_live_engine(self):
        self.assertEqual(stretch.engine_name(), "rubberband")

    def test_stays_on_the_r2_engine(self):
        """R3 ("--fine") is the finer engine in general and this pipeline used
        to pass it, but on our material it smears transients badly: measured
        over percussive catalog tracks across the stretch range, R2 retains 84%
        of the master's attack against R3's 67%. Rendering being offline and
        one-shot is a reason to spend CPU, not a reason to spend it on an
        engine that sounds worse here."""
        self.assertNotIn("--fine", stretch.RUBBERBAND_ARGS)
        self.assertNotIn("-3", stretch.RUBBERBAND_ARGS)

    def test_rubberband_stretches_to_the_requested_duration(self):
        x = synth.synthesize({"id": "1001", "bpm": 124, "key": "8A", "duration_s": 12})
        sr = config.SAMPLE_RATE
        for target in (120, 128):
            ratio = target / 124.0
            out = stretch._stretch_rubberband(x, sr, ratio)
            self.assertAlmostEqual(len(out) / sr, (len(x) / sr) / ratio, delta=0.05,
                                   msg=f"ratio {ratio}")
            self.assertLessEqual(float(np.max(np.abs(out))), 1.0)

    def test_stretch_is_a_no_op_at_unit_ratio(self):
        x = synth.synthesize({"id": "1001", "bpm": 124, "key": "8A", "duration_s": 5})
        np.testing.assert_allclose(stretch.stretch(x, config.SAMPLE_RATE, 1.0), x)


class TestLicenseVersions(unittest.TestCase):
    """Jamendo's catalog is largely CC 3.0; the old mapping hardcoded 4.0."""

    def test_ccurl_keeps_the_real_version(self):
        cases = {
            "http://creativecommons.org/licenses/by/3.0/": "CC BY 3.0",
            "http://creativecommons.org/licenses/by-sa/3.0/": "CC BY-SA 3.0",
            "http://creativecommons.org/licenses/by-nc/3.0/": "CC BY-NC 3.0",
            "http://creativecommons.org/licenses/by-nd/3.0/": "CC BY-ND 3.0",
            "http://creativecommons.org/licenses/by-nc-sa/2.5/": "CC BY-NC-SA 2.5",
            "http://creativecommons.org/licenses/by-nc-nd/3.0/": "CC BY-NC-ND 3.0",
            "https://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
        }
        for url, expected in cases.items():
            self.assertEqual(jamendo._license_from_ccurl(url), expected, url)

    def test_ported_jurisdiction_preserved(self):
        name = jamendo._license_from_ccurl(
            "http://creativecommons.org/licenses/by-sa/2.0/uk/")
        self.assertEqual(name, "CC BY-SA 2.0 UK")
        self.assertEqual(licensing.parse_license(name)["license_url"],
                         "https://creativecommons.org/licenses/by-sa/2.0/uk/")

    def test_flags_are_version_independent(self):
        for version in ("2.5", "3.0", "4.0"):
            self.assertTrue(licensing.parse_license(f"CC BY-NC-ND {version}")["nd"])
            self.assertTrue(licensing.parse_license(f"CC BY-NC-ND {version}")["nc"])
            self.assertFalse(licensing.parse_license(f"CC BY {version}")["nd"])

    def test_unknown_licenses_still_rejected(self):
        for bad in ("CC0", "", None, "CC BY", "CC BY 9.9", "All rights reserved"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                licensing.parse_license(bad)

    def test_unrecognized_ccurl_rejected(self):
        for bad in ("", "https://example.com/whatever",
                    "http://creativecommons.org/publicdomain/zero/1.0/"):
            with self.assertRaises(jamendo.TrackSourceError):
                jamendo._license_from_ccurl(bad)


class TestPcmConversion(unittest.TestCase):
    """Real audio reaches the 16-bit rails; synthesised audio never did."""

    def test_full_scale_negative_maps_to_minus_one(self):
        raw = np.array([-32768, -1, 0, 1, 32767], dtype="<i2").tobytes()
        out = audio_io.pcm16_to_float(raw)
        self.assertEqual(out[0], -1.0)
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)

    def test_wav_round_trip_stays_in_range(self):
        import tempfile
        x = np.linspace(-1.0, 1.0, 4096)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        audio_io.save_wav(path, x, config.SAMPLE_RATE)
        back, sr = audio_io.load_wav(path)
        self.assertEqual(sr, config.SAMPLE_RATE)
        self.assertLessEqual(float(np.max(np.abs(back))), 1.0)
        np.testing.assert_allclose(back, x, atol=1e-4)


class TestCatalogConfig(unittest.TestCase):
    def test_configured_tracks_are_coherent(self):
        cfg = config.load_tracks_config()
        self.assertIn(cfg["mode"], ("jamendo", "offline"))
        self.assertGreaterEqual(len(cfg["tracks"]), 2)
        for t in cfg["tracks"]:
            self.assertIn(t["genre"], config.BPM_BUCKETS, t["id"])
            licensing.parse_license(t["license"])        # raises if unknown
            # Recorded BPM must actually reach its genre's grid, or the track
            # can never be mixed with anything.
            self.assertTrue(bpm_grid.grid_points(t["bpm"], t["genre"]),
                            f"{t['id']} at {t['bpm']} BPM reaches no {t['genre']} grid point")

    def test_catalog_exercises_every_compliance_gate(self):
        """The catalog must keep covering ND/SA/NC, or P1-08..P1-10 stop
        meaning anything in a live ingest."""
        flags = [licensing.parse_license(t["license"])
                 for t in config.load_tracks_config()["tracks"]]
        for gate in ("nd", "sa", "nc"):
            self.assertTrue(any(f[gate] for f in flags), f"no {gate.upper()} track")
        self.assertTrue(any(not any((f["nd"], f["sa"], f["nc"])) for f in flags),
                        "no unrestricted CC BY track")

    def test_catalog_has_a_mixable_pair(self):
        """At least two non-ND tracks must share a grid point, or the app has
        nothing it can actually mix. This is the check that catches a catalog
        swap quietly turning the mixer into a player."""
        cfg = config.load_tracks_config()
        mixable = [t for t in cfg["tracks"]
                   if not licensing.parse_license(t["license"])["nd"]]
        self.assertGreaterEqual(len(mixable), 2, "fewer than two mixable tracks")
        pairs = [
            (a["id"], b["id"])
            for i, a in enumerate(mixable) for b in mixable[i + 1:]
            if a["genre"] == b["genre"]
            and bpm_grid.shared_grid(bpm_grid.grid_points(a["bpm"], a["genre"]),
                                     bpm_grid.grid_points(b["bpm"], b["genre"]))
        ]
        self.assertTrue(pairs, "no two mixable tracks share a BPM grid point")

    def test_tempo_bands_have_no_gaps(self):
        """Regression: bands are integer ranges but detected BPMs are
        continuous. A BPM of 119.96 once fell between the 96-119 and 120-128
        bands and was dropped from the catalog entirely."""
        lo = min(g[0] for g in config.BPM_BUCKETS.values())
        hi = max(g[-1] for g in config.BPM_BUCKETS.values())
        bpm = lo
        while bpm < hi:
            self.assertIsNotNone(config.bucket_for_bpm(bpm),
                                 f"no tempo band covers {bpm:.2f} BPM")
            bpm += 0.01


class TestJamendoCredentials(unittest.TestCase):
    def test_client_id_accepts_either_env_name(self):
        """JAMENDO_API_CLIENT is canonical — it is what Jamendo's dashboard
        calls the field and what .env actually holds — and JAMENDO_CLIENT_ID
        remains an accepted alias for anyone following older docs."""
        saved = {k: os.environ.get(k) for k in config.JAMENDO_CLIENT_ID_VARS}
        try:
            for k in config.JAMENDO_CLIENT_ID_VARS:
                os.environ.pop(k, None)
            self.assertIsNone(config.jamendo_client_id())

            os.environ["JAMENDO_CLIENT_ID"] = "legacy-alias"
            self.assertEqual(config.jamendo_client_id(), "legacy-alias")

            # The dashboard name wins when both are present.
            os.environ["JAMENDO_API_CLIENT"] = "from-dashboard"
            self.assertEqual(config.jamendo_client_id(), "from-dashboard")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_file_does_not_override_real_environment(self):
        os.environ["DJMIXER_ENV_PROBE"] = "from-shell"
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
                f.write("export DJMIXER_ENV_PROBE=from-file\n"
                        "# a comment\n"
                        "DJMIXER_ENV_PROBE_2='quoted value'\n")
                path = f.name
            loaded = config.load_env_file(path)
            self.assertEqual(loaded["DJMIXER_ENV_PROBE"], "from-file")
            self.assertEqual(os.environ["DJMIXER_ENV_PROBE"], "from-shell")
            self.assertEqual(os.environ["DJMIXER_ENV_PROBE_2"], "quoted value")
        finally:
            os.environ.pop("DJMIXER_ENV_PROBE", None)
            os.environ.pop("DJMIXER_ENV_PROBE_2", None)

    def test_missing_credentials_raise_a_clear_error(self):
        saved = {k: os.environ.get(k) for k in config.JAMENDO_CLIENT_ID_VARS}
        try:
            for k in config.JAMENDO_CLIENT_ID_VARS:
                os.environ.pop(k, None)
            with self.assertRaises(jamendo.TrackSourceError) as ctx:
                jamendo.require_client_id()
            self.assertIn("client id", str(ctx.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


@unittest.skipUnless(LIVE and config.jamendo_client_id(),
                     "set DJMIXER_LIVE_TESTS=1 with Jamendo credentials")
class TestJamendoLive(unittest.TestCase):
    """End-to-end against the real Jamendo API (network required)."""

    def test_fetches_metadata_for_every_configured_track(self):
        cfg = config.load_tracks_config()
        client_id = jamendo.require_client_id()
        for entry in cfg["tracks"]:
            t = jamendo.fetch_track_metadata(entry["id"], client_id)
            self.assertEqual(str(t["id"]), str(entry["id"]))
            self.assertTrue(t.get("audiodownload_allowed"),
                            f"{entry['id']} is no longer downloadable")
            name = jamendo._license_from_ccurl(t.get("license_ccurl", ""))
            self.assertEqual(name, entry["license"],
                             f"{entry['id']} license changed upstream")

    def test_full_fetch_decodes_real_audio(self):
        entry = config.load_tracks_config()["tracks"][0]
        meta, samples, sr = jamendo.fetch_track(entry, "jamendo")
        self.assertEqual(sr, config.SAMPLE_RATE)
        self.assertTrue(meta["audiodownload_allowed"])
        self.assertGreater(len(samples) / sr, 30.0)
        self.assertLessEqual(float(np.max(np.abs(samples))), 1.0)
        a = analysis.analyze(samples, sr)
        self.assertGreater(a["bpm"], 40.0)
        self.assertLess(a["bpm"], 200.0)

    def test_download_gate_precedes_download(self):
        """P1-01 must be enforced on live metadata, before any audio transfer."""
        with self.assertRaises(jamendo.TrackSourceError):
            jamendo.validate_source_meta(
                {"id": "x", "audiodownload_allowed": False})


if __name__ == "__main__":
    unittest.main()
