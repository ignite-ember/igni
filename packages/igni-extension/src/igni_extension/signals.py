"""Signals: what the host tells an extension happened, and who handles it.

The host object model (`ext.host`) gives each object signals —
`ext.host.sessions.current_changed`, for one — that carry the new value.
An extension handles one with `@ext.on(signal)`. The objects and the
channel that delivers their signals arrive with the host object model;
this is where their handlers are kept.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class Signal[T]:
    """One signal of one host object. `T` is the value it carries."""

    def __init__(self, name: str, carries: type[T]) -> None:
        self.name = name
        self.carries = carries

    def __repr__(self) -> str:
        return f"Signal({self.name!r})"


class SignalHandlers:
    """Handlers by signal name."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def on[T](
        self, signal: Signal[T]
    ) -> Callable[[Callable[[T], Awaitable[None]]], Callable[[T], Awaitable[None]]]:
        def register(handler: Callable[[T], Awaitable[None]]) -> Callable[[T], Awaitable[None]]:
            self._handlers.setdefault(signal.name, []).append(handler)
            return handler

        return register

    def handlers(self, signal: Signal[Any]) -> list[Handler]:
        return list(self._handlers.get(signal.name, []))

    async def emit[T](self, signal: Signal[T], value: T) -> None:
        """Run every handler of `signal` with `value`, in registration order.

        One handler failing is logged and does not stop the others: they
        are independent reactions to the same event.
        """
        for handler in self.handlers(signal):
            try:
                await handler(value)
            except Exception:
                logger.exception("igni: a handler of %s failed", signal.name)
