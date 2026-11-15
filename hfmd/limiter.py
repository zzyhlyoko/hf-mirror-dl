"""Process-wide token bucket. All curl readers share one bucket so the sum stays near the cap."""

from __future__ import annotations

import threading
import time


class BandwidthLimiter:
    def __init__(self, bits_per_second: float) -> None:
        if bits_per_second <= 0:
            raise ValueError("bandwidth must be positive")
        self.bytes_per_second = bits_per_second / 8.0
        # A short burst absorbs timer jitter. It is not a second budget.
        self.capacity = max(self.bytes_per_second * 0.25, 32 * 1024)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._cv = threading.Condition()

    def acquire(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        remaining = nbytes
        while remaining:
            step = remaining if remaining <= self.capacity else int(self.capacity)
            step = max(1, step)
            self._acquire_one(step)
            remaining -= step

    def refund(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._cv:
            self.tokens = min(self.capacity, self.tokens + nbytes)
            self._cv.notify_all()

    def _acquire_one(self, nbytes: int) -> None:
        with self._cv:
            while True:
                self._refill()
                if self.tokens >= nbytes:
                    self.tokens -= nbytes
                    return
                deficit = nbytes - self.tokens
                wait = deficit / self.bytes_per_second
                self._cv.wait(timeout=max(wait, 0.001))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        self.updated = now
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.bytes_per_second)
