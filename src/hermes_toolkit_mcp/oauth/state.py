"""In-memory state helpers for the embedded OAuth 2.1 server.

Everything the authorization server keeps — registered clients, authorization
codes, tokens, pending login requests — lives in this process. That is a
deliberate choice for a single-user, single-process deployment: there is no
second reader to share with, and a token store on disk would be one more file
an attacker with host access could copy. The flip side is that every store is
bounded and time-limited, so neither a restart nor a flood can leak memory or
leave a live credential behind.

Token values are never stored: callers keep the secret, the stores keep only
``SHA-256(token)``. A dump of process memory therefore yields digests, not
bearer tokens.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")

# Opaque bearer-token prefix: recognisable in a log or a paste, useless to
# anything that is not this process.
TOKEN_PREFIX = "hmt_"


def new_token(prefix: str = TOKEN_PREFIX) -> str:
    """A fresh opaque secret: 256 bits of entropy (RFC 6749 §10.1 needs 128)."""
    return prefix + secrets.token_urlsafe(32)


def new_request_id() -> str:
    """A single-use, unguessable id for a pending login request (128 bits)."""
    return secrets.token_urlsafe(16)


def token_digest(token: str) -> str:
    """The only form of a token we ever persist in a store."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TtlMap(Generic[T]):
    """Bounded, expiring, insertion-ordered map with consuming reads.

    ``take`` removes the entry, which is what single-use objects (authorization
    codes, pending login requests, rotating refresh tokens) require: a code that
    has been exchanged is gone, so replay is answered with "not found" rather
    than with a second token.
    """

    def __init__(self, *, name: str, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.name = name
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[T, float]] = OrderedDict()

    def set(self, key: str, value: T, ttl_seconds: float, *, now: float | None = None) -> None:
        deadline = (now if now is not None else time.time()) + ttl_seconds
        self._entries.pop(key, None)
        self._entries[key] = (value, deadline)
        self._sweep(now)
        while len(self._entries) > self.max_entries:
            # Bound first, freshness second: an anonymous caller (DCR) must
            # never be able to grow this past the cap.
            self._entries.popitem(last=False)

    def get(self, key: str, *, now: float | None = None) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, deadline = entry
        if deadline <= (now if now is not None else time.time()):
            self._entries.pop(key, None)
            return None
        return value

    def take(self, key: str, *, now: float | None = None) -> T | None:
        value = self.get(key, now=now)
        if value is not None:
            self._entries.pop(key, None)
        return value

    def drop(self, key: str) -> None:
        self._entries.pop(key, None)

    def _sweep(self, now: float | None) -> None:
        cutoff = now if now is not None else time.time()
        expired = [key for key, (_value, deadline) in self._entries.items() if deadline <= cutoff]
        for key in expired:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return self.get(str(key)) is not None
