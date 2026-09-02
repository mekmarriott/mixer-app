"""Client-side rate limiting for outbound track-source requests.

Jamendo does not publish a per-second quota. Its API terms reserve the right to
"impose restrictions and limitations on the number and frequency of requests",
and the commonly cited free-tier figure is 35,000 requests/month — a *monthly*
budget with no documented burst rule. Two consequences shape this module:

1. **An unpublished limit is a reason to be more conservative, not less.**
   Defaults here are deliberately slow. A 10,000-track ingest is a background
   job that runs for hours either way; there is nothing to gain by pushing the
   rate up and a live API relationship to lose.

2. **Budget matters more than rate.** Batching is what protects the monthly
   quota: /tracks accepts an array of ids with limit=200, so metadata for
   10,000 tracks costs ~50 requests instead of 10,000. See jamendo.py.

`TokenBucket` paces requests; `RequestBudget` refuses to start work that would
exceed a caller-declared ceiling, so a runaway loop fails loudly and locally
instead of silently eating a month of quota.
"""
from __future__ import annotations

import random
import threading
import time


class TokenBucket:
    """Thread-safe token bucket. `acquire()` blocks until a token is free.

    Shared across worker threads so that raising ingest parallelism raises CPU
    concurrency without raising the outbound request rate — the two are
    deliberately decoupled (see publish.py).
    """

    def __init__(self, rate_per_sec, burst=None, clock=time.monotonic,
                 sleep=time.sleep):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.rate = float(rate_per_sec)
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_sec))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self):
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self, tokens=1):
        """Block until `tokens` are available, then consume them."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            self._sleep(wait)

    def try_acquire(self, tokens=1):
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class BudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    """A hard ceiling on how many requests a run may issue.

    Exists because the expensive failure mode with a monthly quota is not a
    burst, it is a retry loop quietly spending 35,000 requests overnight. The
    budget is checked before each request and raises rather than sleeping.
    """

    def __init__(self, limit):
        self.limit = int(limit)
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self):
        with self._lock:
            return self._used

    @property
    def remaining(self):
        with self._lock:
            return self.limit - self._used

    def spend(self, n=1):
        with self._lock:
            if self._used + n > self.limit:
                raise BudgetExceeded(
                    f"request budget exhausted: {self._used}/{self.limit} used, "
                    f"needed {n} more. Raise --request-budget only if the "
                    f"monthly API quota genuinely allows it.")
            self._used += n
            return self._used


def backoff_delays(attempts=5, base=1.0, cap=60.0, jitter=True, rng=None):
    """Exponential backoff with full jitter, as a list of sleep durations.

    Full jitter (uniform in [0, d]) rather than fixed exponential: when a batch
    of parallel workers all hit a 429 they must not retry in lockstep, which is
    what turns a rate limit into a self-inflicted thundering herd.
    """
    rng = rng or random
    out = []
    for i in range(attempts):
        d = min(cap, base * (2 ** i))
        out.append(rng.uniform(0, d) if jitter else d)
    return out


def retry_after_seconds(headers, default=None):
    """Parse a Retry-After header (delta-seconds form) if the server sent one.

    Always prefer the server's own number to our guess when it is present.
    """
    if not headers:
        return default
    raw = None
    for k in ("Retry-After", "retry-after"):
        if k in headers:
            raw = headers[k]
            break
    if raw is None:
        return default
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return default          # HTTP-date form; fall back to our own backoff
    return max(0.0, v)
