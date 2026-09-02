"""Latency instrumentation: context manager recording per-stage wall time."""
import time
from contextlib import contextmanager


class Timer:
    def __init__(self, database=None):
        """`database` is a backend.db.Database; omit it to measure without
        persisting (the benchmark aggregates in memory either way)."""
        self.database = database
        self.records = []   # (stage, track_id, ms)

    @contextmanager
    def stage(self, name, track_id=None):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            self.records.append((name, track_id, ms))
            if self.database is not None:
                self.database.catalog.record_latency(name, track_id, ms, time.time())

    def by_stage(self):
        agg = {}
        for stage, _tid, ms in self.records:
            agg.setdefault(stage, []).append(ms)
        return {k: {"n": len(v), "mean_ms": sum(v) / len(v),
                    "min_ms": min(v), "max_ms": max(v)} for k, v in agg.items()}
