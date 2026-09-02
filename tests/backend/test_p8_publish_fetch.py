"""The batch publisher's network fetch stage (publish.fetch_masters).

This stage had never run against the live API — the shipped catalog was
ingested through `ingest.py`'s per-track path, and the publisher's own reuse
path serves anything with a metadata sidecar already on disk. So the one line
that actually turns downloaded bytes into a stored master was unexercised, and
wrong.

The failure it produced is the kind worth a permanent test: `fetch_masters`
catches per-track exceptions and logs them, so a fetch that raises for *every*
track still lets the run finish and report success, having stored nothing.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import config, jamendo, publish, storage      # noqa: E402


class FakeLimiter:
    def acquire(self):
        pass


class TestFetchMasters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = storage.LocalBlobStore(root=Path(self.tmp.name))
        self.samples = np.zeros(config.SAMPLE_RATE * 2, dtype=np.float64)

        self._saved = (jamendo.fetch_metadata, jamendo.download_audio,
                       jamendo.decode_to_samples)

        def fetch_metadata(ids, genre_by_id=None, limiter=None, budget=None,
                           **kw):
            return {str(i): {"id": str(i), "name": "n%s" % i, "artist": "a",
                             "genre": "house", "license": "CC BY 3.0",
                             "audiodownload_allowed": True,
                             "audiodownload": "http://audio/%s.mp3" % i,
                             "source_url": ""}
                    for i in ids}

        jamendo.fetch_metadata = fetch_metadata
        jamendo.download_audio = lambda url, limiter=None, **kw: b"encoded"
        # The real signature: the target rate goes IN, only samples come out.
        jamendo.decode_to_samples = lambda encoded, sr=None: self.samples

    def tearDown(self):
        (jamendo.fetch_metadata, jamendo.download_audio,
         jamendo.decode_to_samples) = self._saved

    def fetch(self, entries):
        logged = []
        out = publish.fetch_masters(
            entries, "jamendo", self.store, io_workers=2,
            api_limiter=FakeLimiter(), dl_limiter=FakeLimiter(),
            log=logged.append)
        return out, logged

    def test_downloaded_masters_reach_the_store(self):
        entries = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        out, logged = self.fetch(entries)
        self.assertEqual(len(out), 3)
        for _meta, key in out:
            self.assertTrue(self.store.exists(key), key)

    def test_no_track_fails(self):
        """The bug logged one FAILED line per track and returned nothing, while
        the overall run still exited zero."""
        out, logged = self.fetch([{"id": str(i)} for i in range(5)])
        failures = [line for line in logged if "FAILED" in line]
        self.assertEqual(failures, [])
        self.assertEqual(len(out), 5)

    def test_stored_master_has_the_audio_that_was_decoded(self):
        (_meta, key), = self.fetch([{"id": "1"}])[0]
        from backend.audio_io import load_wav
        samples, sr = load_wav(self.store.local_path(key))
        self.assertEqual(sr, config.SAMPLE_RATE)
        self.assertEqual(len(samples), len(self.samples))


if __name__ == "__main__":
    unittest.main()
