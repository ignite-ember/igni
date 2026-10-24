"""This service's own key: generated once, kept, registered, signed with.

The private half never leaves the service. It is used for two things:

* the client assertion the service authenticates to igni's token
  endpoint with (`private_key_jwt`, RFC 7523) — single use, short-lived;
* the signature over the extension bundle it uploads.
"""

from __future__ import annotations

import asyncio
import logging

from igni_service.clock import Clock
from igni_service.crypto import BundleClaim, RsaKeyPair
from igni_service.keystore import KeyStore, StoredKey

logger = logging.getLogger(__name__)


class SigningKey:
    """The deployment's key, read from its store or made on first start."""

    #: How long a client assertion lives. igni accepts at most 300 s, and
    #: one is minted per token request, so it need only outlive the request.
    ASSERTION_LIFETIME = 60

    def __init__(self, store: KeyStore, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or Clock()
        self._pair: RsaKeyPair | None = None
        self._lock = asyncio.Lock()

    @property
    def store(self) -> KeyStore:
        return self._store

    async def pair(self) -> RsaKeyPair:
        if self._pair is not None:
            return self._pair
        async with self._lock:
            if self._pair is None:
                stored = await self._store.get()
                if stored is None:
                    made = RsaKeyPair.generate()
                    stored = await self._store.claim(
                        StoredKey(kid=made.thumbprint(), private_pem=made.private_pem())
                    )
                    if stored.kid == made.thumbprint():
                        logger.warning("igni: generated a new key pair for this deployment")
                self._pair = RsaKeyPair.from_pem(stored.private_pem)
        return self._pair

    async def thumbprint(self) -> str:
        return (await self.pair()).thumbprint()

    async def assertion(self, *, client_id: str, audience: str) -> str:
        """A `private_key_jwt` client assertion: unique `jti`, a minute to live."""
        pair = await self.pair()
        now = int(self._clock.wall())
        return pair.sign_jwt(
            {
                "iss": client_id,
                "sub": client_id,
                "aud": audience,
                "iat": now,
                "exp": now + self.ASSERTION_LIFETIME,
                "jti": pair.new_jti(),
            }
        )

    async def sign_bundle(self, claim: BundleClaim) -> str:
        """The compact JWS igni checks an uploaded bundle against."""
        return (await self.pair()).sign_bundle(claim)
