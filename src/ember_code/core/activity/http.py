"""HTTP clients that are busy while a request is out.

Every HTTP call igni makes — to a model, to igni server, to a service —
holds a busy lease (:class:`~ember_code.core.activity.registry.ActivityRegistry`)
from the moment it is sent until its answer is read: for a streamed
answer (a model's tokens) until the stream is closed. ``send`` is the
one method every httpx request passes through, so the clients here
override it and nothing else.

A streamed answer nobody closes would hold its lease for ever, so a
streamed lease expires after :attr:`BusyAsyncClient.STREAM_TTL_SECONDS`.

A client used once inside a ``with`` — the usual shape for a call to igni
server — is wrapped by :func:`busy_call` instead, so the code keeps
building ``httpx.AsyncClient`` (which tests stand in for).
``tests/test_background_work_is_accounted_for.py`` fails when a client
is built without either; the reviewed exceptions — a connection held
open while nothing happens, such as the notification stream — are
listed there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from ember_code.core.activity.registry import ActivityRegistry, BusyKind, Lease


def busy_call(holder: str, reason: str = "", *, kind: BusyKind = BusyKind.HTTP_CALL) -> Lease:
    """A lease for one call out, for a ``with`` around a client a test may stand in for.

    async with busy_call("igni-server"), httpx.AsyncClient() as client:
        ...
    """
    return ActivityRegistry.shared().hold(kind, holder, reason)


class _BusyCall:
    """The lease one request holds, and what it is called."""

    #: A streamed answer still open after this no longer counts.
    STREAM_TTL_SECONDS = 15 * 60

    def __init__(self, kind: BusyKind, holder: str, registry: ActivityRegistry | None) -> None:
        self.kind = kind
        self.holder = holder
        self._registry = registry

    def hold(self, request: httpx.Request) -> Lease:
        registry = self._registry if self._registry is not None else ActivityRegistry.shared()
        return registry.hold(self.kind, self.holder, f"{request.method} {request.url.host}")


class _ReleasingAsyncStream(httpx.AsyncByteStream):
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


class _ReleasingSyncStream(httpx.SyncByteStream):
    def __init__(self, inner: Any, lease: Lease) -> None:
        self._inner = inner
        self._lease = lease

    def __iter__(self) -> Iterator[bytes]:
        yield from self._inner

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._lease.release()


class BusyAsyncClient(httpx.AsyncClient):
    """An :class:`httpx.AsyncClient` whose requests are busy leases."""

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

    async def send(self, request: httpx.Request, *, stream: bool = False, **kwargs: Any) -> Any:
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


class BusyClient(httpx.Client):
    """An :class:`httpx.Client` whose requests are busy leases."""

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

    def send(self, request: httpx.Request, *, stream: bool = False, **kwargs: Any) -> Any:
        lease = self._busy.hold(request)
        try:
            response = super().send(request, stream=stream, **kwargs)
        except BaseException:
            lease.release()
            raise
        if not stream or response.is_closed:
            lease.release()
            return response
        lease.renew(self.STREAM_TTL_SECONDS)
        response.stream = _ReleasingSyncStream(response.stream, lease)
        return response


__all__ = ["BusyAsyncClient", "BusyClient", "busy_call"]
