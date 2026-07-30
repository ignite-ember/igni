"""Shared drained-this-run buffer for the queue hooks.

The injector and the persister used to communicate by sharing an
attribute on the injector (``_injected_this_run``). The persister
reached into it, wrote user messages, then called
``clear_injected_this_run()``.

That coupling is now an explicit :class:`InjectedRunBuffer` instance
threaded through :class:`~ember_code.core.hooks.queue.bridge.QueueBridge`
into both hooks. The persister no longer holds an injector reference;
it holds the same buffer instance.

The buffer intentionally exposes both ``snapshot()`` and ``clear()``
rather than an atomic ``drain()``: the persister must write user
messages *before* clearing so a raise mid-append doesn't drop data.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .schemas import InjectedMessage


class InjectedRunBuffer(BaseModel):
    """Ordered list of messages drained during the current run.

    Instance attributes replace what used to be a private ``list[str]``
    on :class:`QueueInjectorHook`, plus a getter property and a
    ``clear_injected_this_run()`` sibling method.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[InjectedMessage] = Field(default_factory=list)

    def append(self, text: str) -> InjectedMessage:
        """Wrap ``text`` in an :class:`InjectedMessage` and append it.

        Returns the created message so callers can log / notify with a
        stable id if they need to.
        """
        message = InjectedMessage(text=text)
        self.items.append(message)
        return message

    def snapshot(self) -> list[InjectedMessage]:
        """Return a shallow copy of the buffer without mutating it.

        The persister uses this to iterate for the append loop, then
        calls :meth:`clear` only after successful persistence — this
        two-step avoids the eager-clear data-loss window an atomic
        ``drain()`` would create.
        """
        return list(self.items)

    def clear(self) -> None:
        """Empty the buffer. Called by the persister after successful
        append, and by the persister's no-op branch to keep the next
        run starting clean."""
        self.items.clear()

    def is_empty(self) -> bool:
        return not self.items

    def __len__(self) -> int:  # convenience for tests / telemetry
        return len(self.items)


__all__ = ["InjectedRunBuffer"]
