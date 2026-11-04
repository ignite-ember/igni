"""igni's public signing keys, cached the way igni says to.

`GET {igni}/.well-known/jwks.json` answers with `Cache-Control: max-age`
(five minutes), and a key is named by its `kid`. The set is kept for
`max-age`, and a token naming a `kid` the set does not have fetches it
again at once — that is how a rotation reaches a service without waiting
out the cache.

**An unknown `kid` refetches at most once per `REFETCH_EVERY`.** The
`kid` is the one part of a token anybody can write without a key, and
without the limit a stream of made-up ones would be a stream of requests
to igni.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from igni_service.clock import Clock
from igni_service.crypto import Jwk, JwkSet
from igni_service.errors import IgniUnavailable
from igni_service.server import IgniServer
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)

_MAX_AGE = re.compile(r"max-age=(\d+)")


class IgniKeys:
    """igni's key set: fetched, kept for `max-age`, refetched for a new `kid`."""

    #: Used when igni sends no `max-age`. igni's own, so the two agree.
    DEFAULT_MAX_AGE = 300.0
    #: The least time between two refetches caused by an unknown `kid`.
    REFETCH_EVERY = 30.0

    def __init__(self, igni: IgniServer, *, clock: Clock | None = None) -> None:
        self._igni = igni
        self._clock = clock or Clock()
        self._keys = JwkSet()
        self._fresh_until = 0.0
        self._last_unknown_refetch = float("-inf")
        self._lock = asyncio.Lock()

    async def key(self, kid: str) -> Jwk | None:
        """The key named `kid`, or `None` when igni does not publish one.

        Raises `IgniUnavailable` only when there is no key set at all —
        neither a cached one nor one igni would hand over — which is
        "cannot check", not "checked and refused".
        """
        if self._clock.monotonic() >= self._fresh_until:
            await self._refresh()
        found = self._keys.find(kid)
        if found is not None:
            return found
        if self._clock.monotonic() - self._last_unknown_refetch < self.REFETCH_EVERY:
            return None
        self._last_unknown_refetch = self._clock.monotonic()
        logger.info("igni keys: a token names kid %r, which is not cached; fetching again", kid)
        await self._refresh(force=True)
        return self._keys.find(kid)

    async def _refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and self._clock.monotonic() < self._fresh_until:
                return
            try:
                response = await self._igni.http.get(self._igni.url(IgniPaths.JWKS))
                response.raise_for_status()
                keys = JwkSet.model_validate(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                if self._keys.keys:
                    # Keep answering from what we have: a key that was
                    # igni's five minutes ago still is, until it says
                    # otherwise. Try again shortly rather than at once.
                    logger.warning(
                        "igni keys: refresh failed (%s); keeping the cached set",
                        type(exc).__name__,
                    )
                    self._fresh_until = self._clock.monotonic() + self.REFETCH_EVERY
                    return
                raise IgniUnavailable(
                    f"could not fetch igni's signing keys: {type(exc).__name__}"
                ) from exc
            self._keys = keys
            self._fresh_until = self._clock.monotonic() + self._max_age(
                response.headers.get("cache-control", "")
            )

    @classmethod
    def _max_age(cls, cache_control: str) -> float:
        match = _MAX_AGE.search(cache_control or "")
        return float(match.group(1)) if match else cls.DEFAULT_MAX_AGE
