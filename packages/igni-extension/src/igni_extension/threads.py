"""Off the event loop: an author's plain function never blocks the extension.

The extension's process serves igni on one event loop: tool calls,
hook events, signals and the host's replies all arrive on it. A tool,
hook handler, reaction or background job written as a plain `def` would
run on that loop and hold every other call until it returns — a 1 s sync
tool would make a hook wait 1 s, and a hook that waits past igni's
deadline counts against the extension. So the SDK runs every plain
function an author hands it in a worker thread
(`anyio.to_thread.run_sync`), and calls `async def` ones as they are.

What this does not do: make blocking code inside an `async def` safe —
that still holds the loop — or make CPU-heavy Python faster: threads
share the GIL. Heavy CPU work belongs in a process pool the extension
runs itself (`concurrent.futures.ProcessPoolExecutor`). The thread pool
is anyio's default limiter (40 threads); it is the extension's process,
so it may change it:

    anyio.to_thread.current_default_thread_limiter().total_tokens = 8
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import partial, wraps
from typing import Any

import anyio.to_thread


class OffTheLoop:
    """Calls an author's function: `async def` on the loop, a plain one in a worker thread."""

    @staticmethod
    def is_async(fn: Callable[..., Any]) -> bool:
        if inspect.iscoroutinefunction(fn):
            return True
        # An object whose `__call__` is `async def`.
        return not inspect.isroutine(fn) and inspect.iscoroutinefunction(type(fn).__call__)

    @classmethod
    async def call(cls, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if cls.is_async(fn):
            return await fn(*args, **kwargs)
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

    @classmethod
    def wrap(cls, fn: Callable[..., Any]) -> Callable[..., Any]:
        """``fn`` as a coroutine function that runs it off the loop when it is plain."""
        if cls.is_async(fn):
            return fn

        @wraps(fn)
        async def off_the_loop(*args: Any, **kwargs: Any) -> Any:
            return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

        return off_the_loop


__all__ = ["OffTheLoop"]
