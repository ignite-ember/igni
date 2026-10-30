"""This extension's token for its own service, fetched from the host.

The extension never holds the person's igni session. The host trades it
for a ten-minute token for this extension's service only and hands that
over on a loopback URL, behind a secret made for this process. This
module asks for it, keeps it until shortly before it expires, and puts
it on every request to the service (`HostTokenAuth`).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from igni_extension.caller import Caller
from igni_extension.clock import Clock
from igni_extension.environment import ServiceEndpoint
from igni_extension.errors import HostError, HostUnavailable, NotConfigured, NotSignedIn


class IssuedToken(BaseModel):
    """What the host's token endpoint answers."""

    access_token: str
    expires_in: float = 600
    service_url: str = ""


class SignedIn(BaseModel):
    """Who the last token was for, read without checking it.

    For showing who is signed in and noticing that it changed. The service
    checks the token; this side never decides anything with what it says.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = ""
    org_id: str = ""
    email: str = ""
    name: str = ""

    @classmethod
    def of(cls, token: str) -> SignedIn:
        try:
            payload = token.split(".")[1]
            claims: dict[str, Any] = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
        except (IndexError, ValueError, TypeError):
            return cls()
        return cls(
            user_id=str(claims.get("sub") or ""),
            org_id=str(claims.get("org") or ""),
            email=str(claims.get("email") or ""),
            name=str(claims.get("name") or ""),
        )


class HostTokens:
    """Tokens for this extension's service, from the host that started it."""

    #: How long before expiry a kept token is replaced, in seconds.
    RENEW_BEFORE = 30.0

    def __init__(
        self,
        endpoint: ServiceEndpoint,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        timeout: float = 10.0,
        caller: Caller | None = None,
    ) -> None:
        self._endpoint = endpoint
        #: Whom these tokens are for, named to the host; `None` is the
        #: signed-in person, as before callers existed.
        self._caller = caller
        self._for: dict[str, HostTokens] = {}
        self._transport = transport
        self._clock = clock or Clock()
        self._timeout = timeout
        self._token = ""
        self._expires_at = 0.0
        self._signed_in: SignedIn | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._endpoint.token_url and self._endpoint.token_secret)

    @property
    def signed_in(self) -> SignedIn | None:
        """Who the last token was for; `None` before one, or once signed out."""
        return self._signed_in

    def for_(self, caller: Caller) -> HostTokens:
        """Tokens for the service as `caller`: one set per caller, each kept apart.

        The host is told whom a token is for and hands out only one it
        may: on a backend that serves one person, that person's — a
        token for anybody else is refused (`HostError`).
        """
        found = self._for.get(caller.ref)
        if found is None:
            found = HostTokens(
                self._endpoint,
                transport=self._transport,
                clock=self._clock,
                timeout=self._timeout,
                caller=caller,
            )
            self._for[caller.ref] = found
        return found

    def invalidate(self) -> None:
        """Forget the kept token; the next request asks the host again."""
        self._token = ""
        self._expires_at = 0.0

    async def token(self) -> str:
        """A token for the service. Raises `NotSignedIn`, `NotConfigured`, `HostUnavailable`."""
        if not self.configured:
            raise NotConfigured(
                "igni did not give this process a token endpoint for its service "
                "(IGNI_SERVICE_TOKEN_URL, IGNI_SERVICE_TOKEN_SECRET)"
            )
        if self._fresh():
            return self._token
        async with self._lock:
            if self._fresh():
                return self._token
            return await self._fetch()

    def _caller_ref(self) -> str:
        return self._caller.ref if self._caller is not None else "the signed-in person"

    def _fresh(self) -> bool:
        return bool(self._token) and self._clock.monotonic() < self._expires_at - self.RENEW_BEFORE

    async def _fetch(self) -> str:
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            try:
                headers = {"Authorization": f"Bearer {self._endpoint.token_secret}"}
                if self._caller is not None:
                    headers[Caller.TOKEN_HEADER] = self._caller.ref
                response = await client.get(self._endpoint.token_url, headers=headers)
            except httpx.HTTPError as exc:
                raise HostUnavailable(
                    f"the igni host did not answer: {type(exc).__name__}"
                ) from exc
        if response.status_code == 503:
            self.invalidate()
            self._signed_in = None
            raise NotSignedIn()
        if response.status_code == 403:
            raise HostError(
                "refused", f"the igni host will not give a token for {self._caller_ref()}"
            )
        if response.status_code != 200:
            raise HostUnavailable(f"the igni host answered {response.status_code}")
        try:
            issued = IssuedToken.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise HostUnavailable("the igni host answered with something else") from exc
        self._token = issued.access_token
        self._expires_at = self._clock.monotonic() + issued.expires_in
        self._signed_in = SignedIn.of(issued.access_token)
        return self._token


class HostTokenAuth(httpx.Auth):
    """Puts the person's token on every request to the service.

    A 401 from the service means the token stopped being good earlier than
    its expiry said, so it is dropped and the request is sent once more
    with a fresh one.
    """

    def __init__(self, tokens: HostTokens) -> None:
        self._tokens = tokens

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._tokens.token()}"
        response = yield request
        if response.status_code == 401:
            self._tokens.invalidate()
            request.headers["Authorization"] = f"Bearer {await self._tokens.token()}"
            yield request
