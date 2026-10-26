"""The one way igni starts work in the background.

Every task or thread igni starts goes through :class:`BackgroundTasks`,
so none can be busy without the :class:`ActivityRegistry` knowing:

* :meth:`BackgroundTasks.start` — work that finishes. It holds a busy
  lease from start to end, so a process with such a task going is not
  suspended under it;
* :meth:`BackgroundTasks.daemon` — a loop that lives as long as what
  owns it (a reader, a poll, a supervisor). It holds no lease, or igni
  would never be idle; what it *does* when it wakes holds its own;
* :meth:`BackgroundTasks.thread` — a thread, the same two ways.

Both keep a reference to what they start until it ends, which a bare
``create_task`` does not: a task nobody references may be collected
mid-flight. ``tests/test_background_work_is_accounted_for.py`` fails when
code under ``src/`` starts a task or thread any other way.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ClassVar, TypeVar, overload

from ember_code.core.activity.registry import ActivityRegistry, BusyKind, Lease

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BackgroundTasks:
    """Starts tasks and threads, and accounts for them."""

    _shared: ClassVar[BackgroundTasks | None] = None

    def __init__(self, registry: ActivityRegistry | None = None) -> None:
        self._registry = registry
        self._tasks: set[asyncio.Future[Any]] = set()
        self._lock = threading.Lock()

    @classmethod
    def shared(cls) -> BackgroundTasks:
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @property
    def registry(self) -> ActivityRegistry:
        return self._registry if self._registry is not None else ActivityRegistry.shared()

    @overload
    def start(
        self,
        work: Coroutine[Any, Any, T],
        *,
        holder: str,
        reason: str = ...,
        kind: BusyKind = ...,
        name: str | None = ...,
        loop: asyncio.AbstractEventLoop | None = ...,
    ) -> asyncio.Task[T]: ...
    @overload
    def start(
        self,
        work: Awaitable[T],
        *,
        holder: str,
        reason: str = ...,
        kind: BusyKind = ...,
        name: str | None = ...,
        loop: asyncio.AbstractEventLoop | None = ...,
    ) -> asyncio.Future[T]: ...
    def start(
        self,
        work: Awaitable[T],
        *,
        holder: str,
        reason: str = "",
        kind: BusyKind = BusyKind.BACKGROUND,
        name: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Future[T]:
        """Run ``work`` in the background, busy until it ends.

        A coroutine is wrapped in an :class:`asyncio.Task`; any other awaitable
        comes back as an :class:`asyncio.Future` — the overloads above say which,
        so callers that keep the handle to ``cancel()`` it get the precise type.
        """
        lease = self.registry.hold(kind, holder, reason)
        try:
            future = self._schedule(work, name=name or holder, loop=loop)
        except BaseException:
            lease.release()
            raise
        future.add_done_callback(lambda _f: lease.release())
        return future

    @overload
    def daemon(
        self,
        work: Coroutine[Any, Any, T],
        *,
        name: str,
        loop: asyncio.AbstractEventLoop | None = ...,
    ) -> asyncio.Task[T]: ...
    @overload
    def daemon(
        self,
        work: Awaitable[T],
        *,
        name: str,
        loop: asyncio.AbstractEventLoop | None = ...,
    ) -> asyncio.Future[T]: ...
    def daemon(
        self,
        work: Awaitable[T],
        *,
        name: str,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Future[T]:
        """Run a long-lived loop in the background. It does not count as busy."""
        return self._schedule(work, name=name, loop=loop)

    def start_if_running(
        self,
        work: Coroutine[Any, Any, T],
        *,
        holder: str,
        reason: str = "",
        kind: BusyKind = BusyKind.BACKGROUND,
    ) -> asyncio.Task[T] | None:
        """:meth:`start` when a loop is running here; otherwise drop ``work``, closed."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            work.close()
            return None
        return self.start(work, holder=holder, reason=reason, kind=kind)

    def thread(
        self,
        target: Callable[[], Any],
        *,
        name: str,
        counts: bool = False,
        reason: str = "",
    ) -> threading.Thread:
        """Start a daemon thread; ``counts`` holds a lease while it runs."""
        lease: Lease | None = (
            self.registry.hold(BusyKind.BACKGROUND, name, reason) if counts else None
        )

        def run() -> None:
            try:
                target()
            finally:
                if lease is not None:
                    lease.release()

        worker = threading.Thread(target=run, name=name, daemon=True)
        worker.start()
        return worker

    async def drain(self) -> None:
        """Wait for everything started here; for tests and shutdown."""
        while True:
            with self._lock:
                pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _schedule(
        self,
        work: Awaitable[T],
        *,
        name: str,
        loop: asyncio.AbstractEventLoop | None,
    ) -> asyncio.Future[T]:
        if asyncio.iscoroutine(work):
            target_loop = loop if loop is not None else asyncio.get_running_loop()
            future: asyncio.Future[T] = target_loop.create_task(work, name=name)
        else:
            future = asyncio.ensure_future(work, loop=loop)
        with self._lock:
            self._tasks.add(future)
        future.add_done_callback(self._forget)
        return future

    def _forget(self, future: asyncio.Future[Any]) -> None:
        with self._lock:
            self._tasks.discard(future)


#: The process's one instance.
background = BackgroundTasks.shared()

__all__ = ["BackgroundTasks", "background"]
