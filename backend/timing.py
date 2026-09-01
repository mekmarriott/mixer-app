"""Latency instrumentation: context manager recording per-stage wall time."""
import time
from contextlib import contextmanager


class Timer:
    def __init__(self, con=None):
        self.con = con
        self.records = []   # (stage, track_id, ms)

    @contextmanager
    def stage(self, name, track_id=None):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            self.records.append((name, track_id, ms))
            if self.con is not None:
                from . import db
                db.record_latency(self.con, name, track_id, ms, time.time())

    def by_stage(self):
        agg = {}
        for stage, _tid, ms in self.records:
            agg.setdefault(stage, []).append(ms)
        return {k: {"n": len(v), "mean_ms": sum(v) / len(v),
                    "min_ms": min(v), "max_ms": max(v)} for k, v in agg.items()}
