"""Session management — leaks the raw token in error responses.

Tagged in the eval as ``vulnerabilities=[token-leak]``,
``security=major-issues``, ``domain=[auth]``.
"""

from __future__ import annotations

from src.cache.lru import LRUCache

_session_cache: LRUCache = LRUCache(maxsize=1024)


def store_session(token: str, user_id: str) -> None:
    _session_cache.put(token, user_id)


def resolve_session(token: str) -> str:
    user_id = _session_cache.get(token)
    if user_id is None:
        # Reflecting the raw token back to the caller — token leak.
        raise ValueError(f"Unknown session token: {token}")
    return user_id


def revoke_session(token: str) -> None:
    _session_cache.evict(token)
