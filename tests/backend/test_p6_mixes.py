"""Saved mixes: chain integrity, the overlap invariant, and CRUD.

MIX-01  the chain walks in order and rejects cycles/orphans
MIX-02  at most two tracks may overlap — enforced on write, not just in the UI
MIX-03  a drag is one row, one column, and is validated the same way
MIX-04  CRUD: create, list (most recent first), rename, load, delete
MIX-05  beats are the stored unit, so off-grid placement is unrepresentable
"""
import unittest

from fixture import get_fixture

from backend import mixes
from backend.app import create_app


class FakeRow:
    def __init__(self, id, next_id=None, track_id="t", delta_beats=0, grid_bpm=120):
        self.id, self.next_id = id, next_id
        self.track_id, self.delta_beats, self.grid_bpm = track_id, delta_beats, grid_bpm


class TestChain(unittest.TestCase):
    def test_mix_01_walk_orders_by_next_id(self):
        rows = [FakeRow("b", "c"), FakeRow("c", None), FakeRow("a", "b")]
        self.assertEqual([r.id for r in mixes.walk(rows, "a")], ["a", "b", "c"])

    def test_mix_01_walk_rejects_a_cycle(self):
        rows = [FakeRow("a", "b"), FakeRow("b", "a")]
        with self.assertRaises(mixes.ChainError) as ctx:
            mixes.walk(rows, "a")
        self.assertIn("cycle", str(ctx.exception))

    def test_mix_01_walk_rejects_an_orphan(self):
        """A row nothing points at is silent data loss if ignored."""
        rows = [FakeRow("a", None), FakeRow("stranded", None)]
        with self.assertRaises(mixes.ChainError) as ctx:
            mixes.walk(rows, "a")
        self.assertIn("unreachable", str(ctx.exception))

    def test_mix_01_walk_rejects_a_dangling_reference(self):
        with self.assertRaises(mixes.ChainError):
            mixes.walk([FakeRow("a", "ghost")], "a")

    def test_mix_01_empty_mix_walks_to_nothing(self):
        self.assertEqual(mixes.walk([], None), [])

    # ------------------------------------------------------------- MIX-05
    def test_mix_05_beats_round_trip_through_seconds(self):
        for bpm in (120, 124, 128, 90):
            for beats in (0, 1, 16, 96, 512):
                secs = mixes.beats_to_seconds(beats, bpm)
                self.assertEqual(mixes.seconds_to_beats(secs, bpm), beats)

    def test_mix_05_seconds_quantize_to_whole_beats(self):
        """An off-grid gap cannot survive the conversion — that is the point."""
        bpm = 124
        beat = 60.0 / bpm
        self.assertEqual(mixes.seconds_to_beats(beat * 8 + 0.12, bpm), 8)
        self.assertEqual(mixes.seconds_to_beats(beat * 8 - 0.12, bpm), 8)

    def test_mix_05_zero_bpm_is_not_a_division_error(self):
        self.assertEqual(mixes.beats_to_seconds(4, 0), 0.0)
        self.assertEqual(mixes.seconds_to_beats(4, 0), 0)


class TestOverlapInvariant(unittest.TestCase):
    """MIX-02 — at most two tracks on the grid at any instant."""

    @staticmethod
    def entries(starts, duration=60.0):
        return [{"track_id": f"t{i}", "offset_s": s, "duration_s": duration,
                 "delta_s": s - (starts[i - 1] if i else 0), "grid_bpm": 120}
                for i, s in enumerate(starts)]

    def test_mix_02_neighbours_may_overlap(self):
        # That IS the crossfade.
        mixes.check_overlaps(self.entries([0, 45, 90]))

    def test_mix_02_a_third_track_may_not_reach_the_first(self):
        with self.assertRaises(mixes.ChainError) as ctx:
            mixes.check_overlaps(self.entries([0, 20, 40]))   # t2 starts inside t0
        self.assertIn("3 tracks on the grid", str(ctx.exception))

    def test_mix_02_exact_abutment_is_legal(self):
        """Track 2 starting exactly as track 0 ends is two tracks, not three."""
        mixes.check_overlaps(self.entries([0, 30, 60]))

    def test_mix_02_checks_every_window_not_just_the_first(self):
        with self.assertRaises(mixes.ChainError):
            mixes.check_overlaps(self.entries([0, 60, 120, 150, 170]))

    def test_mix_02_short_chains_are_always_legal(self):
        mixes.check_overlaps(self.entries([0]))
        mixes.check_overlaps(self.entries([0, 5]))     # heavy overlap, only 2 tracks

    # ------------------------------------------------------------- MIX-03
    def test_mix_03_min_delta_keeps_a_drag_inside_the_invariant(self):
        entries = self.entries([0, 45, 90])
        floor = mixes.min_delta_beats(entries, 2)
        # Track 2 may not start before track 0 ends (60s) -> at least 15s after
        # track 1's start, which at 120 BPM is 30 beats.
        self.assertEqual(floor, 30)

    def test_mix_03_first_track_has_no_floor(self):
        self.assertEqual(mixes.min_delta_beats(self.entries([0, 45]), 0), 0)


class TestMixApi(unittest.TestCase):
    """MIX-04 — CRUD through the real API."""

    @classmethod
    def setUpClass(cls):
        cls.database, _, cls.tmp = get_fixture()
        app = create_app(run_ingestion=False, database=cls.database)
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        for m in self.client.get("/api/mixes").get_json():
            self.client.delete(f"/api/mixes/{m['id']}")

    def _tracks(self):
        return [t["id"] for t in self.client.get("/api/tracks").get_json()]

    def test_mix_04_create_list_rename_delete(self):
        self.assertEqual(self.client.get("/api/mixes").get_json(), [])

        made = self.client.post("/api/mixes", json={"name": "Warm-up"}).get_json()
        self.assertEqual(made["track_count"], 0)

        listed = self.client.get("/api/mixes").get_json()
        self.assertEqual([m["name"] for m in listed], ["Warm-up"])

        self.client.patch(f"/api/mixes/{made['id']}", json={"name": "Peak Time"})
        self.assertEqual(self.client.get("/api/mixes").get_json()[0]["name"], "Peak Time")

        self.assertEqual(self.client.delete(f"/api/mixes/{made['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/mixes").get_json(), [])

    def test_mix_04_listed_most_recently_edited_first(self):
        a = self.client.post("/api/mixes", json={"name": "First"}).get_json()
        b = self.client.post("/api/mixes", json={"name": "Second"}).get_json()
        self.client.patch(f"/api/mixes/{a['id']}", json={"name": "First again"})
        names = [m["name"] for m in self.client.get("/api/mixes").get_json()]
        self.assertEqual(names[0], "First again", f"got {names}")
        self.assertIn("Second", names)

    def test_mix_04_chain_round_trips_with_derived_offsets(self):
        ids = self._tracks()
        m = self.client.post("/api/mixes", json={"name": "Set"}).get_json()
        r = self.client.put(f"/api/mixes/{m['id']}/tracks", json={"tracks": [
            {"track_id": ids[0], "delta_beats": 0, "grid_bpm": 124},
            {"track_id": ids[1], "delta_beats": 96, "grid_bpm": 124},
        ]})
        self.assertEqual(r.status_code, 200)

        got = self.client.get(f"/api/mixes/{m['id']}").get_json()
        self.assertEqual([t["track_id"] for t in got["tracks"]], ids[:2])
        self.assertEqual(got["tracks"][0]["offset_s"], 0.0)
        self.assertAlmostEqual(got["tracks"][1]["offset_s"],
                               96 * 60.0 / 124, places=6)
        self.assertEqual(self.client.get("/api/mixes").get_json()[0]["track_count"], 2)

    def test_mix_03_drag_writes_one_row(self):
        ids = self._tracks()
        m = self.client.post("/api/mixes", json={"name": "Drag"}).get_json()
        self.client.put(f"/api/mixes/{m['id']}/tracks", json={"tracks": [
            {"track_id": ids[0], "delta_beats": 0, "grid_bpm": 124},
            {"track_id": ids[1], "delta_beats": 96, "grid_bpm": 124},
        ]})
        got = self.client.get(f"/api/mixes/{m['id']}").get_json()
        node = got["tracks"][1]["node_id"]

        r = self.client.patch(f"/api/mixes/{m['id']}/tracks/{node}",
                              json={"delta_beats": 80})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["delta_beats"], 80)

        after = self.client.get(f"/api/mixes/{m['id']}").get_json()
        # The node id is stable across a move — ordering did not change.
        self.assertEqual(after["tracks"][1]["node_id"], node)
        self.assertEqual(after["tracks"][0]["node_id"], got["tracks"][0]["node_id"])

    def test_mix_02_api_refuses_a_three_way_overlap(self):
        """The rule is enforced server-side: the UI is not the only guard."""
        ids = self._tracks()
        m = self.client.post("/api/mixes", json={"name": "Bad"}).get_json()
        r = self.client.put(f"/api/mixes/{m['id']}/tracks", json={"tracks": [
            {"track_id": ids[0], "delta_beats": 0, "grid_bpm": 124},
            {"track_id": ids[1], "delta_beats": 41, "grid_bpm": 124},
            {"track_id": ids[2], "delta_beats": 41, "grid_bpm": 124},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("3 tracks on the grid", r.get_json()["detail"])

    def test_mix_02_drag_cannot_create_a_three_way_overlap(self):
        ids = self._tracks()
        m = self.client.post("/api/mixes", json={"name": "Drag bad"}).get_json()
        self.client.put(f"/api/mixes/{m['id']}/tracks", json={"tracks": [
            {"track_id": ids[0], "delta_beats": 0, "grid_bpm": 124},
            {"track_id": ids[1], "delta_beats": 96, "grid_bpm": 124},
            {"track_id": ids[2], "delta_beats": 96, "grid_bpm": 124},
        ]})
        got = self.client.get(f"/api/mixes/{m['id']}").get_json()
        r = self.client.patch(f"/api/mixes/{m['id']}/tracks/{got['tracks'][2]['node_id']}",
                              json={"delta_beats": 10})
        self.assertEqual(r.status_code, 409)

    def test_mix_04_unknown_mix_is_404(self):
        self.assertEqual(self.client.get("/api/mixes/nope").status_code, 404)

    def test_mix_04_rename_requires_a_name(self):
        m = self.client.post("/api/mixes", json={}).get_json()
        self.assertEqual(m["name"], "Untitled Mix")
        self.assertEqual(
            self.client.patch(f"/api/mixes/{m['id']}", json={"name": "  "}).status_code,
            400)

    def test_mix_04_empty_mix_loads_as_the_zero_state(self):
        m = self.client.post("/api/mixes", json={"name": "Empty"}).get_json()
        got = self.client.get(f"/api/mixes/{m['id']}").get_json()
        self.assertEqual(got["tracks"], [])


if __name__ == "__main__":
    unittest.main()


class TestMixListingCost(unittest.TestCase):
    """MIX-06 — listing mixes must not cost a query per mix.

    The picker is refreshed after every save, so an O(mixes) listing makes the
    app slower the longer someone uses it.
    """

    @classmethod
    def setUpClass(cls):
        cls.database, _, cls.tmp = get_fixture()
        app = create_app(run_ingestion=False, database=cls.database)
        app.config["TESTING"] = True
        cls.client = app.test_client()
        cls.repo = app.config["MIXES"]

    def setUp(self):
        for m in self.client.get("/api/mixes").get_json():
            self.client.delete(f"/api/mixes/{m['id']}")

    def test_mix_06_listing_is_flat_in_the_number_of_mixes(self):
        seen = []
        real = self.repo.database.reading

        def counting():
            seen.append(1)
            return real()

        for n in (2, 12):
            for m in self.client.get("/api/mixes").get_json():
                self.client.delete(f"/api/mixes/{m['id']}")
            for i in range(n):
                self.client.post("/api/mixes", json={"name": f"m{i}"})

            seen.clear()
            self.repo.database.reading = counting
            try:
                listed = self.repo.list()
            finally:
                self.repo.database.reading = real

            self.assertEqual(len(listed), n)
            # One read scope, whatever the mix count.
            self.assertEqual(len(seen), 1, f"{len(seen)} read scopes for {n} mixes")

    def test_mix_06_counts_are_still_correct(self):
        ids = [t["id"] for t in self.client.get("/api/tracks").get_json()]
        empty = self.client.post("/api/mixes", json={"name": "empty"}).get_json()
        full = self.client.post("/api/mixes", json={"name": "full"}).get_json()
        self.client.put(f"/api/mixes/{full['id']}/tracks", json={"tracks": [
            {"track_id": ids[0], "delta_beats": 0, "grid_bpm": 124},
            {"track_id": ids[1], "delta_beats": 96, "grid_bpm": 124},
        ]})
        counts = {m["name"]: m["track_count"] for m in self.client.get("/api/mixes").get_json()}
        self.assertEqual(counts["empty"], 0)
        self.assertEqual(counts["full"], 2)
