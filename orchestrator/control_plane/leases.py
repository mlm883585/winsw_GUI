from __future__ import annotations

import threading
import time
from collections.abc import Callable


class MonotonicLeaseRegistry:
    """Process-local freshness proof for accepted Agent reports.

    Persisted wall-clock timestamps are retained for observability, but only a
    tick recorded by this CP process may authorize an online-only action.  A new
    process therefore starts fail-closed until every Agent sends a fresh,
    accepted report.
    """

    def __init__(
        self,
        lease_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = float(lease_seconds)
        self._monotonic = monotonic
        self._ticks: dict[str, float] = {}
        self._lock = threading.RLock()

    def renew(self, subject: object) -> None:
        """Record freshness after the corresponding report commits durably."""

        tick = self._monotonic()
        with self._lock:
            self._ticks[str(subject)] = tick

    def is_online(self, subject: object) -> bool:
        current = self._monotonic()
        with self._lock:
            received = self._ticks.get(str(subject))
            if received is None:
                return False
            # A monotonic clock should never move backwards.  If an injected or
            # platform clock violates that invariant, fail closed and require a
            # new accepted report rather than extending an unprovable lease.
            if current < received:
                self._ticks.pop(str(subject), None)
                return False
            return current - received < self.lease_seconds

    def clear(self, subject: object) -> None:
        with self._lock:
            self._ticks.pop(str(subject), None)
