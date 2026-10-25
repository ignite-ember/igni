"""`ext.service`: this extension's own service, as the signed-in person.

An `httpx.AsyncClient` whose paths are relative to the service and whose
every request carries the person's token for it — fetched from the igni
host, renewed before it expires, retried once on a 401
(`igni_extension.tokens`). Two more ways to reach the same service:

* `ext.service.anonymous` — the same service with no token, for what
  anyone may read: a public link, a health check. A request through it
  never asks the host for a token, so it works when nobody is signed in.
* `ext.service.websocket(path)` — a WebSocket to the service, opened
  with the person's token in its `Authorization` header:

      async with ext.service.websocket("/v1/rooms/runbook") as room:
          await room.send(json.dumps({"hello": "room"}))
          async for frame in room: ...

`ext.tokens.token()` is the token itself, for a client this does not
cover; `ext.tokens.signed_in` says whose it was.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

from igni_extension.caller import Caller
from igni_extension.tokens import HostTokenAuth, HostTokens


class ServiceWebSocket:
    """`async with ext.service.websocket(path) as socket:` — opened as the person."""

    def __init__(self, url: str, tokens: HostTokens, options: dict[str, Any]) -> None:
        self.url = url
        self._tokens = tokens
        self._options = options
        self._socket: ClientConnection | None = None

    async def __aenter__(self) -> ClientConnection:
        token = await self._tokens.token()
        headers = dict(self._options.pop("additional_headers", None) or {})
        headers["Authorization"] = f"Bearer {token}"
        self._socket = await connect(self.url, additional_headers=headers, **self._options)
        return self._socket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None


class ServiceClient(httpx.AsyncClient):
    """`ext.service`: the extension's service, as the signed-in person."""

    def __init__(
        self,
        *,
        base_url: str,
        tokens: HostTokens,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            auth=HostTokenAuth(tokens),
            transport=transport,
            timeout=timeout,
        )
        self._tokens = tokens
        self._transport_override = transport
        self._timeout_seconds = timeout
        self._anonymous: httpx.AsyncClient | None = None
        self._for: dict[str, ServiceClient] = {}

    def for_(self, caller: Caller) -> ServiceClient:
        """The same service, as `caller`: what a tool acting for someone uses.

        The token is fetched from the host for that caller, and kept apart
        from every other caller's. The service sees exactly who the call
        is for, as it checks every token.
        """
        found = self._for.get(caller.ref)
        if found is None:
            found = ServiceClient(
                base_url=str(self.base_url),
                tokens=self._tokens.for_(caller),
                transport=self._transport_override,
                timeout=self._timeout_seconds,
            )
            self._for[caller.ref] = found
        return found

    @property
    def anonymous(self) -> httpx.AsyncClient:
        """The same service, with no token: for what anyone may read."""
        if self._anonymous is None:
            self._anonymous = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport_override,
                timeout=self._timeout_seconds,
            )
        return self._anonymous

    def websocket(self, path: str, **options: Any) -> ServiceWebSocket:
        """A WebSocket to `path` on the service, with the person's token.

        `options` go to `websockets.asyncio.client.connect`.
        """
        base = str(self.base_url).rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return ServiceWebSocket(f"{base}/{path.lstrip('/')}", self._tokens, options)

    async def aclose(self) -> None:
        for client in self._for.values():
            await client.aclose()
        self._for.clear()
        if self._anonymous is not None:
            await self._anonymous.aclose()
            self._anonymous = None
        await super().aclose()


__all__ = ["ServiceClient", "ServiceWebSocket"]
