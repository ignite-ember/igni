"""igni server, as a service reaches it.

Deployed with one address for igni and nothing else, a service learns
the rest from igni: the issuer every token carries, from
`/.well-known/openid-configuration`, and igni's signing keys, from
`/.well-known/jwks.json` (`igni_service.jwks`).

**Calls go to the configured address; the issuer is only compared.**
igni's metadata names its endpoints under its public URL, which in a
deployment is often not the address a neighbouring container reaches it
at. So the discovery document is asked for one fact — the `issuer`
tokens are checked against and client assertions are addressed to — and
every call goes to the configured address plus the paths the wire fixes.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic import BaseModel

from igni_service.errors import IgniUnavailable
from igni_service.urls import IgniPaths, ServiceUrl


class Discovery(BaseModel):
    """The parts of igni's metadata a service relies on."""

    issuer: str
    #: The portal's sign-in page, where a person's browser is sent (§5).
    #: The one URL from the metadata that is used as published: the
    #: browser, not this service, goes there.
    authorization_endpoint: str = ""


class IgniServer:
    """igni's address, its issuer, this service's own URL, and one HTTP client."""

    def __init__(
        self,
        *,
        base_url: str,
        own_url: str,
        timeout: float = 10.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._own_url = ServiceUrl.normalise(own_url)
        self._timeout = timeout
        self._http = http
        self._owns_http = http is None
        self._discovery: Discovery | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def own_url(self) -> str:
        """This service's URL as igni writes it: the `aud` of every token it accepts."""
        return self._own_url

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    def url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def discovery(self) -> Discovery:
        """igni's metadata, read once and kept for the life of the process.

        The issuer is igni's public URL, and changes only with a
        redeployment of igni — which no token already in circulation would
        survive either.
        """
        if self._discovery is not None:
            return self._discovery
        async with self._lock:
            if self._discovery is None:
                try:
                    response = await self.http.get(self.url(IgniPaths.DISCOVERY))
                    response.raise_for_status()
                    discovered = Discovery.model_validate(response.json())
                except (httpx.HTTPError, ValueError) as exc:
                    raise IgniUnavailable(
                        f"could not read igni server's metadata: {type(exc).__name__}"
                    ) from exc
                self._discovery = discovered.model_copy(
                    update={"issuer": discovered.issuer.rstrip("/")}
                )
        return self._discovery

    async def issuer(self) -> str:
        """igni's `issuer`: every token's `iss`, and every client assertion's `aud`."""
        return (await self.discovery()).issuer

    async def authorization_endpoint(self) -> str:
        """Where a person's browser goes to sign in to this service."""
        endpoint = (await self.discovery()).authorization_endpoint
        if not endpoint:
            raise IgniUnavailable("igni server's metadata names no authorization_endpoint")
        return endpoint

    async def aclose(self) -> None:
        """Close the HTTP client, if this object made it."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
