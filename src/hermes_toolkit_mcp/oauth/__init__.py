"""Embedded OAuth 2.1 authorization server for the Streamable-HTTP transport.

Contract: ``docs/oauth-contract.md`` (locked), machine-checked by
``tests/test_oauth_contract.py``. Modules:

* ``state``      — bounded TTL stores, token generation and digests (shared)
* ``ratelimit``  — per-caller sliding windows for the anonymous endpoints
* ``discovery``  — RFC 9728 / RFC 8414 metadata and every path shape
* ``provider``   — ``OAuthAuthorizationServerProvider``: clients, codes, tokens
* ``login``      — the single-user login form that completes ``/authorize``

Nothing here is imported from another package: ``http_server`` is the only
caller, so the transport decides which parts of the flow exist.
"""
