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

#: Peers whose address is authoritative: they connected directly to this
#: loopback listener, so any forwarded header they sent is their own claim.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})


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
    """Coarse caller identity for rate limiting.

    Two rules, in order:

    1. **A loopback peer is its own key.** Anything reaching this listener
       directly (local process, container via the host) chose its own
       ``X-Forwarded-For`` header, so honouring it would let that caller pick
       its rate-limit bucket — or burn a stranger's. From loopback the only
       honest identity is the socket peer.
    2. **Otherwise take the last ``X-Forwarded-For`` entry.** Traefik on this
       box runs with the default trust-all ``forwardedHeaders``, which
       *appends* the address it saw to whatever the client already sent: the
       first entry is the caller's claim, the last is the proxy's observation.
       Absent the header, fall back to the socket peer (never to an empty
       string, which would merge every such caller into one bucket).
    """
    peer = (request.client.host if request.client else "") or ""
    if peer in _LOOPBACK_PEERS:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    if peer:
        return peer
    return "unknown"
