"""In-process token-bucket rate limiter. Per (bucket, key) — key is the user, device or client IP.

Limitation (documented): state lives in one process's memory, so it does not coordinate across
several workers or hosts and resets on restart; a production deployment should also rate-limit at
the reverse proxy / API gateway.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, limits: dict, clock=time.monotonic):
        self.limits = dict(limits)                # bucket -> (capacity, per_seconds)
        self._now = clock
        self._lock = threading.Lock()
        self._state: dict = {}                    # (bucket, key) -> [tokens, last]
        self.violations: dict = {}

    def allow(self, bucket: str, key: str) -> tuple:
        """-> (allowed, retry_after_s)."""
        if bucket not in self.limits:
            return True, 0.0
        cap, per = self.limits[bucket]
        rate = cap / per
        now = self._now()
        with self._lock:
            st = self._state.get((bucket, key))
            if st is None:
                st = self._state[(bucket, key)] = [float(cap), now]
            tokens = min(cap, st[0] + (now - st[1]) * rate)
            st[1] = now
            if tokens >= 1.0:
                st[0] = tokens - 1.0
                return True, 0.0
            st[0] = tokens
            self.violations[bucket] = self.violations.get(bucket, 0) + 1
            if len(self._state) > 20000:
                for k in list(self._state)[:10000]:
                    del self._state[k]
            return False, round((1.0 - tokens) / rate, 2)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()
            self.violations.clear()
