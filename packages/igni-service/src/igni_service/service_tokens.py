"""This service's own tokens for igni, one per organisation.

`client_credentials` with `private_key_jwt` and `organisation=<org>`:
every token names one organisation, and the directory and notifications
answer for that one only. So a token is kept per organisation and
renewed `RENEW_BEFORE` ahead of its expiry rather than at it — a call
made with a token about to expire can reach igni after it has.

A 401 from igni with a token that should still be good means igni has
stopped honouring it (the service was revoked, or igni's keys moved):
`request` drops it and tries once more with a fresh one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel

from igni_service.clock import Clock
from igni_service.errors import IgniUnavailable
from igni_service.registration import ServiceRegistration
from igni_service.server import IgniServer
from igni_service.signing import SigningKey
from igni_service.token_endpoint import GrantRefused, IssuedTokens, TokenEndpoint


class HeldToken(BaseModel):
    token: str
    expires_at: float


class ServiceTokens:
    """Service tokens for igni, kept per organisation."""

    #: How long before expiry a kept token is replaced, in seconds.
    RENEW_BEFORE = 60.0

    def __init__(
        self,
        igni: IgniServer,
        registration: ServiceRegistration,
        key: SigningKey,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._igni = igni
        self._endpoint = TokenEndpoint(igni, registration, key)
        self._clock = clock or Clock()
        self._held: dict[str, HeldToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def forget(self, org_id: str | None = None) -> None:
        if org_id is None:
            self._held.clear()
        else:
            self._held.pop(org_id, None)

    async def token(self, org_id: str) -> str:
        """A service token for `org_id`. Raises `IgniUnavailable`."""
        if not org_id:
            raise IgniUnavailable("a service token is for one organisation, and none was named")
        held = self._fresh(org_id)
        if held is not None:
            return held
        lock = self._locks.setdefault(org_id, asyncio.Lock())
        async with lock:
            held = self._fresh(org_id)
            if held is not None:
                return held
            issued = await self._issue(org_id)
            self._held[org_id] = HeldToken(
                token=issued.access_token,
                expires_at=self._clock.monotonic() + issued.expires_in,
            )
            return issued.access_token

    def _fresh(self, org_id: str) -> str | None:
        held = self._held.get(org_id)
        if held is not None and self._clock.monotonic() < held.expires_at - self.RENEW_BEFORE:
            return held.token
        return None

    async def _issue(self, org_id: str) -> IssuedTokens:
        try:
            return await self._endpoint.grant(
                {"grant_type": "client_credentials", "organisation": org_id}
            )
        except GrantRefused as exc:
            # `invalid_client` while a super admin has not approved this
            # service yet, or after they revoked it. Said in igni's own
            # words, because that is the sentence to act on.
            raise IgniUnavailable(f"igni server refused a service token: {exc}") from exc

    async def request(
        self, method: str, path: str, *, org_id: str, **kwargs: Any
    ) -> httpx.Response:
        """One call to igni as this service, for `org_id`. Raises `IgniUnavailable`.

        Answers whatever igni answered, error statuses included, except a
        401 — which is retried once with a new token, and raised if it
        happens again.
        """
        extra_headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        for attempt in (1, 2):
            token = await self.token(org_id)
            try:
                response = await self._igni.http.request(
                    method,
                    self._igni.url(path),
                    headers={**extra_headers, "Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise IgniUnavailable(f"could not reach igni server: {type(exc).__name__}") from exc
            if response.status_code != 401:
                return response
            self.forget(org_id)
            if attempt == 2:
                break
        raise IgniUnavailable(f"igni server refused this service for organisation {org_id}")
