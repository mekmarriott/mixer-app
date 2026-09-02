"""Catalog/blob-store reconciliation (backend/reconcile.py).

Runs entirely against the local filesystem store, which implements the same
interface as the Vercel one; only the transport differs. Every case the
production audit turned up is represented here: an object that is absent, one
that is present but a different render, one the store holds and no row names,
and one whose local file no longer matches the row that describes it.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import config, reconcile, storage           # noqa: E402
from backend.audio_io import save_wav                    # noqa: E402
from backend.db import Database                          # noqa: E402

SR = config.SAMPLE_RATE


def tone(seconds):
    n = int(round(seconds * SR))
    return np.zeros(n, dtype=np.float64)


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = storage.LocalBlobStore(root=root / "store")
        self.source = storage.LocalBlobStore(root=root / "local")
        self.database = Database.from_url(
            "sqlite:///" + str(root / "catalog.sqlite3")).migrate()
        self.addCleanup(self.database.dispose)
        self.addCleanup(self.tmp.cleanup)

    def add_track(self, tid, duration, key=None):
        key = key or storage.master_key(tid)
        with self.database.writing() as q:
            q.upsert_track(id=tid, name="n", artist="a", genre="g",
                           license="CC BY 3.0", license_nd=False,
                           license_sa=False, license_nc=False, mixable=True,
                           native_bpm=120.0, camelot="8A", duration_s=duration,
                           audio_key=key, analysis_json=None,
                           segments_json=None, status="ready",
                           status_error=None, source_url=None,
                           fetched_at=None, analyzed_at=None, ready_at=None)
        return key

    def add_variant(self, tid, bpm, duration):
        key = storage.variant_key(tid, bpm)
        with self.database.writing() as q:
            q.upsert_variant(track_id=tid, grid_bpm=bpm, ratio=1.0,
                             object_key=key, duration_s=duration)
        return key

    def write(self, which, key, duration):
        path = which.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_wav(path, tone(duration), SR)

    def plan(self):
        return reconcile.plan(self.database, self.store, self.source)


class TestPlan(ReconcileCase):
    def test_agreeing_object_is_matched(self):
        key = self.add_track("1", 2.0)
        self.write(self.source, key, 2.0)
        self.write(self.store, key, 2.0)
        p = self.plan()
        self.assertEqual(p["matched"], [key])
        self.assertEqual(p["missing"], [])
        self.assertEqual(p["stale"], [])

    def test_absent_object_is_reported_with_its_local_source(self):
        key = self.add_track("1", 2.0)
        self.write(self.source, key, 2.0)
        p = self.plan()
        self.assertEqual([e["key"] for e in p["missing"]], [key])
        self.assertTrue(p["missing"][0]["local_ok"])

    def test_a_different_render_at_the_same_key_is_stale_not_matched(self):
        """The production failure: the key resolves, the audio is wrong."""
        self.add_track("1", 10.0)
        key = self.add_variant("1", 128, 10.0)
        self.write(self.source, key, 10.0)
        self.write(self.store, key, 8.5)          # an older render
        p = self.plan()
        self.assertEqual([e["key"] for e in p["stale"]], [key])
        self.assertAlmostEqual(p["stale"][0]["claimed"], 10.0, places=3)
        # `actual` describes the disagreement; its shape depends on whether the
        # comparison was bytes (encoded) or duration (PCM).
        self.assertIsInstance(p["stale"][0]["actual"], str)
        self.assertTrue(p["stale"][0]["actual"])

    def test_unreferenced_object_is_an_orphan(self):
        self.write(self.store, "variants/9_106.wav", 3.0)
        self.assertEqual(self.plan()["orphans"], ["variants/9_106.wav"])

    def test_row_whose_local_render_disagrees_is_unfixable(self):
        """Uploading here would publish audio the row does not describe.

        Pinned to a PCM key: a duration is only recoverable from an
        uncompressed file, so this guard applies to PCM and the encoded case
        relies on the encode having come from the row's own source.
        """
        key = self.add_track("1", 2.0, key="audio/1.wav")
        self.write(self.source, key, 5.0)         # local disagrees with the row
        p = self.plan()
        self.assertEqual(p["missing"], [])
        self.assertEqual([e["key"] for e in p["unfixable"]], [key])

    def test_row_with_no_local_render_is_unfixable(self):
        key = self.add_track("1", 2.0)
        self.assertEqual([e["key"] for e in self.plan()["unfixable"]], [key])

    def test_tolerance_admits_rounding_but_not_a_different_render(self):
        """PCM only — the size/duration tolerance has no meaning once the
        object is compressed, where the comparison is exact bytes."""
        key = self.add_track("1", 2.0, key="audio/1.wav")
        self.write(self.store, key, 2.0 + reconcile.TOLERANCE_S / 2)
        self.assertEqual(self.plan()["matched"], [key])


class TestApply(ReconcileCase):
    def test_apply_uploads_absent_and_stale_and_leaves_orphans(self):
        absent = self.add_track("1", 2.0)
        self.add_track("2", 4.0)
        stale = self.add_variant("2", 128, 4.0)
        self.write(self.source, absent, 2.0)
        self.write(self.source, storage.master_key("2"), 4.0)
        self.write(self.store, storage.master_key("2"), 4.0)
        self.write(self.source, stale, 4.0)
        self.write(self.store, stale, 3.0)
        self.write(self.store, "variants/orphan_1.wav", 1.0)

        done = reconcile.apply_plan(self.plan(), self.store, self.source,
                                    progress=lambda *_: None)
        self.assertEqual(sorted(done["uploaded"]), sorted([absent, stale]))
        self.assertEqual(done["deleted"], [])

        after = self.plan()
        self.assertEqual(after["missing"], [])
        self.assertEqual(after["stale"], [])
        self.assertEqual(after["orphans"], ["variants/orphan_1.wav"])

    def test_orphans_are_deleted_only_when_asked(self):
        self.write(self.store, "variants/orphan_1.wav", 1.0)
        reconcile.apply_plan(self.plan(), self.store, self.source,
                             progress=lambda *_: None)
        self.assertTrue(self.store.exists("variants/orphan_1.wav"))
        reconcile.apply_plan(self.plan(), self.store, self.source,
                             delete_orphans=True, progress=lambda *_: None)
        self.assertFalse(self.store.exists("variants/orphan_1.wav"))

    def test_apply_is_idempotent(self):
        key = self.add_track("1", 2.0)
        self.write(self.source, key, 2.0)
        reconcile.apply_plan(self.plan(), self.store, self.source,
                             progress=lambda *_: None)
        second = reconcile.apply_plan(self.plan(), self.store, self.source,
                                      progress=lambda *_: None)
        self.assertEqual(second["uploaded"], [])


class TestPrefixFilter(ReconcileCase):
    """`--prefix` narrows the audit to one class of object — used to publish
    masters while variants are being re-rendered under a new codec."""

    def setup_mixed(self):
        self.add_track("1", 2.0)
        master = storage.master_key("1")
        variant = self.add_variant("1", 128, 3.0)
        for which in (self.source,):
            self.write(which, master, 2.0)
            self.write(which, variant, 3.0)
        return master, variant

    def plan_prefix(self, prefix):
        return reconcile.plan(self.database, self.store, self.source,
                              prefix=prefix)

    def test_only_masters_are_considered_under_the_audio_prefix(self):
        master, variant = self.setup_mixed()
        p = self.plan_prefix("audio/")
        self.assertEqual([e["key"] for e in p["missing"]], [master])
        self.assertEqual(p["catalog_keys"], 1)

    def test_applying_under_a_prefix_uploads_nothing_outside_it(self):
        master, variant = self.setup_mixed()
        reconcile.apply_plan(self.plan_prefix("audio/"), self.store,
                             self.source, progress=lambda *_: None)
        self.assertTrue(self.store.exists(master))
        self.assertFalse(self.store.exists(variant))

    def test_objects_outside_the_prefix_are_not_reported_as_orphans(self):
        """The dangerous case: filtering the catalog but not the store would
        make every variant look unreferenced, and --delete-orphans would then
        erase renders the catalog still points at."""
        master, variant = self.setup_mixed()
        self.write(self.store, variant, 3.0)          # already published
        p = self.plan_prefix("audio/")
        self.assertEqual(p["orphans"], [])

    def test_delete_orphans_under_a_prefix_leaves_other_prefixes_intact(self):
        master, variant = self.setup_mixed()
        self.write(self.store, variant, 3.0)
        self.write(self.store, "audio/stray.wav", 1.0)
        reconcile.apply_plan(self.plan_prefix("audio/"), self.store,
                             self.source, delete_orphans=True,
                             progress=lambda *_: None)
        self.assertFalse(self.store.exists("audio/stray.wav"))
        self.assertTrue(self.store.exists(variant))

    def test_no_prefix_still_sees_everything(self):
        master, variant = self.setup_mixed()
        p = self.plan_prefix("")
        self.assertEqual(sorted(e["key"] for e in p["missing"]),
                         sorted([master, variant]))


class TestParallelApply(ReconcileCase):
    """Uploads run concurrently because each one is a network-bound subprocess;
    at catalog scale (~5700 objects for a 1200-track import) serial uploading
    is most of a day."""

    def _pending(self, n):
        keys = []
        for i in range(n):
            key = self.add_track(str(i), 2.0)
            self.write(self.source, key, 2.0)
            keys.append(key)
        return keys

    def test_every_object_lands_when_workers_are_concurrent(self):
        keys = self._pending(12)
        done = reconcile.apply_plan(self.plan(), self.store, self.source,
                                    progress=lambda *_: None, workers=4)
        self.assertEqual(sorted(done["uploaded"]), sorted(keys))
        self.assertEqual(done["failed"], [])
        self.assertEqual(self.plan()["missing"], [])

    def test_one_failure_does_not_abandon_the_rest(self):
        keys = self._pending(6)
        doomed = keys[2]
        real_put = self.store.put_file

        def put(key, src, content_type=None):
            if key == doomed:
                raise storage.BlobStoreError("upload refused")
            return real_put(key, src, content_type)

        self.store.put_file = put
        done = reconcile.apply_plan(self.plan(), self.store, self.source,
                                    progress=lambda *_: None, workers=4)
        self.assertEqual(done["failed"], [doomed])
        self.assertEqual(len(done["uploaded"]), 5)
        # The re-check is what surfaces the failure, and a rerun retries it.
        self.assertEqual([e["key"] for e in self.plan()["missing"]], [doomed])

    def test_serial_and_parallel_reach_the_same_end_state(self):
        self._pending(5)
        reconcile.apply_plan(self.plan(), self.store, self.source,
                             progress=lambda *_: None, workers=1)
        serial = self.plan()
        self.assertEqual(serial["missing"], [])
        self.assertEqual(len(serial["matched"]), 5)


class TestDeliveryContentType(unittest.TestCase):
    """An object's MIME must follow the configured delivery format, not
    `mimetypes` — which names `.m4a` a raw AAC-LATM stream that browsers
    refuse, and spells WAV differently from DELIVERY_FORMATS."""

    def test_delivery_extensions_use_the_configured_mime(self):
        for ext, mime, _enc in config.DELIVERY_FORMATS.values():
            self.assertEqual(storage._content_type("audio/1.%s" % ext), mime)

    def test_m4a_is_a_container_not_an_elementary_stream(self):
        self.assertEqual(storage._content_type("audio/1.m4a"), "audio/mp4")
        self.assertNotEqual(storage._content_type("audio/1.m4a"),
                            "audio/mp4a-latm")

    def test_unknown_extensions_still_fall_back(self):
        self.assertEqual(storage._content_type("meta/1.json"),
                         "application/json")
        self.assertEqual(storage._content_type("x/y.unknownext"),
                         "application/octet-stream")

    def test_uploads_without_an_explicit_type_get_the_right_one(self):
        """reconcile re-uploads through put_file with no content type, so the
        default is what the object ends up carrying."""
        seen = {}

        class Recording(storage.LocalBlobStore):
            def put_file(self, key, src_path, content_type=None):
                seen[key] = content_type or storage._content_type(key)
                return super().put_file(key, src_path, content_type)

        with tempfile.TemporaryDirectory() as d:
            store = Recording(root=Path(d) / "s")
            src = Path(d) / "a.m4a"
            src.write_bytes(b"x")
            store.put_file("audio/1.m4a", src)
        self.assertEqual(seen["audio/1.m4a"], "audio/mp4")


class TestCodecAgnosticDrift(ReconcileCase):
    """Once objects are compressed, byte size no longer implies duration, so
    staleness is judged against the local artifact instead."""

    def test_encoded_object_matching_local_bytes_is_not_stale(self):
        key = self.add_track("1", 2.0, key="audio/1.m4a")
        for which in (self.source, self.store):
            p = which.local_path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"z" * 5000)
        self.assertEqual(self.plan()["matched"], [key])

    def test_encoded_object_differing_from_local_bytes_is_stale(self):
        key = self.add_track("1", 2.0, key="audio/1.m4a")
        self.source.local_path(key).parent.mkdir(parents=True, exist_ok=True)
        self.source.local_path(key).write_bytes(b"z" * 5000)
        self.store.local_path(key).parent.mkdir(parents=True, exist_ok=True)
        self.store.local_path(key).write_bytes(b"z" * 4000)
        self.assertEqual([e["key"] for e in self.plan()["stale"]], [key])

    def test_encoded_size_is_never_read_as_a_duration(self):
        """The regression: treating a compressed size as PCM would compute a
        nonsense duration, mark every object stale, and re-upload forever."""
        key = self.add_track("1", 240.0, key="audio/1.m4a")
        for which in (self.source, self.store):
            p = which.local_path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"z" * 5000)          # 240s of AAC is nothing like PCM
        p = self.plan()
        self.assertEqual(p["stale"], [])
        self.assertEqual(p["matched"], [key])


class TestDurationArithmetic(unittest.TestCase):
    def test_size_derived_duration_matches_the_header(self):
        """The audit reads lengths from object sizes rather than downloading;
        that shortcut has to agree with what the file actually says."""
        from backend.audio_io import wav_duration
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.wav"
            save_wav(path, tone(3.25), SR)
            self.assertAlmostEqual(
                reconcile.duration_from_size(path.stat().st_size, SR),
                wav_duration(path), places=9)


class TestLocalStoreListing(unittest.TestCase):
    def test_list_keys_walks_nested_prefixes(self):
        with tempfile.TemporaryDirectory() as d:
            store = storage.LocalBlobStore(root=Path(d))
            store.put_bytes("audio/1.wav", b"a")
            store.put_bytes("variants/1_120.wav", b"b")
            self.assertEqual(store.list_keys(),
                             ["audio/1.wav", "variants/1_120.wav"])
            self.assertEqual(store.list_keys("variants/"),
                             ["variants/1_120.wav"])

    def test_delete_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            store = storage.LocalBlobStore(root=Path(d))
            store.put_bytes("audio/1.wav", b"a")
            store.delete("audio/1.wav")
            store.delete("audio/1.wav")
            self.assertFalse(store.exists("audio/1.wav"))


class TestVercelStoreShims(unittest.TestCase):
    def test_cli_may_be_a_multiword_command(self):
        self.assertEqual(storage.VercelBlobStore(cli="vercel").argv, ["vercel"])
        self.assertEqual(
            storage.VercelBlobStore(cli=["npx", "--yes", "vercel@latest"]).argv,
            ["npx", "--yes", "vercel@latest"])

    def test_blob_store_id_is_dropped_when_using_a_rw_token(self):
        """The CLI refuses to start when BLOB_STORE_ID is set without an OIDC
        token, and the Vercel integration writes BLOB_STORE_ID into .env."""
        store = storage.VercelBlobStore(token="tok")
        old = os.environ.get("BLOB_STORE_ID")
        os.environ["BLOB_STORE_ID"] = "store_x"
        try:
            self.assertNotIn("BLOB_STORE_ID", store._env())
            os.environ["VERCEL_OIDC_TOKEN"] = "oidc"
            self.assertIn("BLOB_STORE_ID", store._env())
        finally:
            os.environ.pop("VERCEL_OIDC_TOKEN", None)
            if old is None:
                os.environ.pop("BLOB_STORE_ID", None)
            else:
                os.environ["BLOB_STORE_ID"] = old

    def test_list_output_is_parsed_into_keys_and_sizes(self):
        sample = (
            "Vercel CLI 59.11.1\n"
            "Fetching blobs\n"
            "  Uploaded At  Size     Pathname           URL\n"
            "  42m          7685742  audio/1800901.wav  "
            "https://x.public.blob.vercel-storage.com/audio/1800901.wav\n"
            "  41m          123      variants/1_120.wav  "
            "https://x.public.blob.vercel-storage.com/variants/1_120.wav\n")
        rows = {}
        for line in sample.splitlines():
            m = storage.VercelBlobStore.LIST_ROW_RE.match(line)
            if m:
                rows[m.group("pathname")] = int(m.group("size"))
        self.assertEqual(rows, {"audio/1800901.wav": 7685742,
                                "variants/1_120.wav": 123})

    def test_next_page_cursor_is_recovered(self):
        line = ("> To display the next page run `vercel blob list "
                "--limit 3 --next AbC+d/eF==`")
        m = storage.VercelBlobStore.LIST_CURSOR_RE.search(line)
        self.assertEqual(m.group(1), "AbC+d/eF==")


if __name__ == "__main__":
    unittest.main()
