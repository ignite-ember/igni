"""The busy client of :mod:`.http`, on ``httpx2``.

The ``anthropic`` SDK from 1.0 on is built on ``httpx2``, the
API-compatible continuation of ``httpx``, and refuses an ``httpx``
client: ``http_client=`` must be an ``httpx2.AsyncClient``. This is
:class:`~ember_code.core.activity.http.BusyAsyncClient` for it — the same
lease for each request, held until a streamed answer is closed — so a
call to Claude is busy exactly as a call to any other model is.

Imported only by the ``anthropic`` provider, and only when it builds a
model: ``httpx2`` arrives with the SDK, and an install without the SDK
must still start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2

from ember_code.core.activity.http import _BusyCall
from ember_code.core.activity.registry import ActivityRegistry, BusyKind, Lease


class _ReleasingAsyncStream(httpx2.AsyncByteStream):
    def __init__(self, inner: Any, lease: Lease) -> None:
        self._inner = inner
        self._lease = lease

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._lease.release()


class BusyAsyncClient(httpx2.AsyncClient):
    """An :class:`httpx2.AsyncClient` whose requests are busy leases."""

    STREAM_TTL_SECONDS = _BusyCall.STREAM_TTL_SECONDS

    def __init__(
        self,
        *args: Any,
        busy_holder: str,
        busy_kind: BusyKind = BusyKind.HTTP_CALL,
        busy_registry: ActivityRegistry | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._busy = _BusyCall(busy_kind, busy_holder, busy_registry)

    async def send(self, request: httpx2.Request, *, stream: bool = False, **kwargs: Any) -> Any:
        lease = self._busy.hold(request)
        try:
            response = await super().send(request, stream=stream, **kwargs)
        except BaseException:
            lease.release()
            raise
        if not stream or response.is_closed:
            lease.release()
            return response
        lease.renew(self.STREAM_TTL_SECONDS)
        response.stream = _ReleasingAsyncStream(response.stream, lease)
        return response

    def renewed(self, *, limits: Any) -> BusyAsyncClient:
        """A new client with this one's lease, auth and timeout.

        What :class:`~ember_code.backend.model_http_client_manager.ModelHttpClientManager`
        installs after a run. ``limits`` may be an ``httpx.Limits``; its
        three numbers are what is kept.
        """
        return BusyAsyncClient(
            busy_holder=self._busy.holder,
            busy_kind=self._busy.kind,
            busy_registry=self._busy.registry,
            auth=self.auth,
            timeout=self.timeout,
            limits=httpx2.Limits(
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
            ),
        )


__all__ = ["BusyAsyncClient"]
