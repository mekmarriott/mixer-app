"""Incompatible licences are rejected BEFORE the audio is fetched (LIC-01..05).

An ND licence prohibits derivative works, and every BPM-grid variant is one
(requirements.md §2), so an ND track can never be mixed. Ingesting one anyway
means downloading it over a metered API, analysing it, and then permanently
excluding it from the only feature the catalogue exists for.

On the live catalogue 64 of 72 tracks are ND, so this is most of the work and
most of the quota.
"""
import unittest

import teststore

from backend import config

teststore.isolate(config)

from backend import ingest, jamendo, licensing, synth  # noqa: E402
from backend.db import Database  # noqa: E402

BY = {"id": "7001", "name": "Open", "artist": "A", "genre": "house",
      "bpm": 124, "key": "8A", "license": "CC BY 4.0", "duration_s": 12}
ND = {"id": "7002", "name": "Closed", "artist": "B", "genre": "house",
      "bpm": 124, "key": "8A", "license": "CC BY-ND 4.0", "duration_s": 12}
NC_ND = {"id": "7003", "name": "Closed too", "artist": "C", "genre": "house",
         "bpm": 124, "key": "8A", "license": "CC BY-NC-ND 4.0", "duration_s": 12}
SA = {"id": "7004", "name": "Share", "artist": "D", "genre": "house",
      "bpm": 124, "key": "8A", "license": "CC BY-SA 4.0", "duration_s": 12}


class LicenseGateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = teststore.isolate(config)
        self.database = Database.from_config().migrate()
        self._synth = synth.synthesize
        self.made = []
        synth.synthesize = self._counting

    def tearDown(self):
        synth.synthesize = self._synth
        self.database.dispose()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _counting(self, entry):
        self.made.append(entry["id"])
        return self._synth(entry)

    def ingest(self, entry, **kw):
        return ingest.ingest_track(self.database, entry, "offline", **kw)

    def summaries(self):
        with self.database.reading() as q:
            return {t.id: t for t in q.list_track_summaries()}

    # -------------------------------------------------------------- LIC-01
    def test_lic_01_no_audio_is_produced_for_an_nd_track(self):
        """The whole point: the expensive part never runs."""
        result = self.ingest(ND)
        self.assertEqual(result["skipped"], "license")
        self.assertFalse(result["mixable"])
        self.assertEqual(self.made, [], "audio was fetched for an ND track")

    def test_lic_01_a_usable_track_is_still_ingested_fully(self):
        result = self.ingest(BY)
        self.assertNotIn("skipped", result)
        self.assertTrue(result["mixable"])
        self.assertEqual(self.made, [BY["id"]])
        self.assertTrue(result["grid_bpms"])

    def test_lic_01_every_nd_variant_is_rejected(self):
        for entry in (ND, NC_ND):
            with self.subTest(entry["license"]):
                self.assertEqual(self.ingest(entry)["skipped"], "license")
        self.assertEqual(self.made, [])

    def test_lic_01_non_nd_restrictions_do_not_block_ingestion(self):
        """SA and NC restrict distribution and monetisation, not derivation.

        They are flagged for later handling (P1-09, P1-10), not skipped —
        rejecting them would throw away usable material.
        """
        result = self.ingest(SA)
        self.assertTrue(result["mixable"])
        self.assertIn(SA["id"], self.made)

    # -------------------------------------------------------------- LIC-02
    def test_lic_02_the_licence_is_still_recorded(self):
        """requirements.md §1: the specific CC variant is stored per track,
        whether or not the track is usable."""
        self.ingest(ND)
        row = self.summaries()[ND["id"]]
        self.assertEqual(row.license, "CC BY-ND 4.0")
        self.assertTrue(row.license_nd)
        self.assertFalse(row.mixable)

    def test_lic_02_no_variants_exist_for_a_skipped_track(self):
        self.ingest(ND)
        with self.database.reading() as q:
            self.assertEqual(q.list_variants_for_track(track_id=ND["id"]), [])

    def test_lic_02_a_skipped_track_carries_no_audio_key(self):
        self.ingest(ND)
        with self.database.reading() as q:
            self.assertIsNone(q.get_track(id=ND["id"]).audio_key)

    # -------------------------------------------------------------- LIC-03
    def test_lic_03_the_decision_is_not_relitigated_on_restart(self):
        """A licence does not change between runs, so the second pass must not
        even make the metadata request."""
        self.ingest(ND)
        calls = []
        real = jamendo.fetch_track
        jamendo.fetch_track = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            result = self.ingest(ND)
        finally:
            jamendo.fetch_track = real
        self.assertEqual(result["skipped"], "license")
        self.assertEqual(calls, [], "re-checked a licence it had already stored")

    def test_lic_03_force_re_examines_the_licence(self):
        """`force` exists for when the catalogue itself was wrong."""
        self.ingest(ND)
        calls = []
        real = jamendo.fetch_track
        jamendo.fetch_track = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            self.ingest(ND, force=True)
        finally:
            jamendo.fetch_track = real
        self.assertEqual(len(calls), 1)

    # -------------------------------------------------------------- LIC-04
    def test_lic_04_the_gate_runs_before_the_download_not_after(self):
        """Ordering is the entire value: a gate after the fetch saves nothing.

        Exercised through the real fetch_track, so the hook is proven to be
        called at the point the network path calls it.
        """
        seen = []
        with self.assertRaises(jamendo.IncompatibleLicense):
            jamendo.fetch_track(ND, "offline",
                                accept=lambda m: (seen.append(m["license"]),
                                                  ingest._reject_unmixable_license(m)))
        self.assertEqual(seen, ["CC BY-ND 4.0"])
        self.assertEqual(self.made, [], "audio was produced despite the refusal")

    def test_lic_04_the_hook_is_optional(self):
        meta, samples, sr = jamendo.fetch_track(BY, "offline")
        self.assertEqual(meta["id"], BY["id"])
        self.assertTrue(len(samples))

    def test_lic_04_the_refusal_says_why(self):
        with self.assertRaises(jamendo.IncompatibleLicense) as ctx:
            ingest._reject_unmixable_license(
                {"id": "x", "license": "CC BY-ND 4.0"})
        msg = str(ctx.exception)
        self.assertIn("derivative", msg)
        self.assertIn("before download", msg)

    # -------------------------------------------------------------- LIC-05
    def test_lic_05_an_unknown_licence_still_raises(self):
        """P1-07: an unrecognised licence is an error, not a silent skip."""
        with self.assertRaises(Exception) as ctx:
            ingest._reject_unmixable_license({"id": "x", "license": "WTFPL"})
        self.assertNotIsInstance(ctx.exception, jamendo.IncompatibleLicense)

    def test_lic_05_skipped_tracks_are_hidden_from_the_deck(self):
        """End to end with the presentation rule they feed."""
        from backend import deck
        self.ingest(ND)
        self.ingest(BY)
        rows = [{"id": t.id, "genre": t.genre, "mixable": t.mixable,
                 "status": t.status} for t in self.summaries().values()]
        shown = [t["id"] for g in deck.genre_groups(rows) for t in g["tracks"]]
        self.assertEqual(shown, [BY["id"]])


if __name__ == "__main__":
    unittest.main()
