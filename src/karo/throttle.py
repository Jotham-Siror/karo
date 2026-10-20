"""Collapses a flood of forged posts into one audit row a window, carrying how many there were."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta


class RejectionThrottle:
    """Counts rejected posts. The posts held back since the last row are counted in the next one."""

    def __init__(self, window: timedelta) -> None:
        self._window = window
        self._lock = threading.Lock()
        self._last_row: datetime | None = None
        self._unrecorded = 0

    def take(self, now: datetime) -> int | None:
        """Counts one post. Returns the count for a new audit row, or None if a row was written inside the window."""
        with self._lock:
            self._unrecorded += 1
            if self._last_row is not None and now - self._last_row < self._window:
                return None
            count = self._unrecorded
            self._last_row = now
            self._unrecorded = 0
            return count
