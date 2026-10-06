"""Per-key sliding-window rate limiting for the anonymous OAuth endpoints.

The login form and dynamic client registration are the two places an
unauthenticated caller can spend server effort, so both are limited. This is
in-memory on purpose: one process, one clock, no shared state to configure.

Keys are coarse (client address), which is the right granularity behind a
reverse proxy — see ``client_key``.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

from starlette.requests import Request


class SlidingWindowLimiter:
    """Allow ``limit`` events per key inside ``window_seconds``."""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 4096) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt and report whether it is within the limit."""
        moment = time.time() if now is None else now
        window = self._hits.get(key)
        if window is None:
            window = deque()
            self._hits[key] = window
            self._evict_keys()
        self._hits.move_to_end(key)
        cutoff = moment - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(moment)
        return True

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Seconds until the oldest attempt in the window ages out (>= 1)."""
        moment = time.time() if now is None else now
        window = self._hits.get(key)
        if not window:
            return 1
        remaining = window[0] + self.window_seconds - moment
        return max(1, int(-(-remaining // 1)))

    def _evict_keys(self) -> None:
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)

    def __len__(self) -> int:
        return len(self._hits)


def client_key(request: Request) -> str:
    """Coarse caller identity: the address Traefik recorded for us.

    Traefik is the only thing that can reach this loopback listener, so the
    first ``X-Forwarded-For`` entry it wrote is the real client; a direct
    loopback caller without the header falls back to its own address.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
