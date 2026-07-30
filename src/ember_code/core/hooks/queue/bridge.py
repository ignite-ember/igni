"""Coordinator that owns the buffer and manufactures both hooks.

The old ``create_queue_hook`` free function that returned a
``(injector, persister)`` tuple is retired in favour of an explicit
:class:`QueueBridge` coordinator. The class makes the implicit
subject of ``queue_hook.py`` explicit and centralises the shared
:class:`InjectedRunBuffer` instance both hooks read/write.

Typical usage in :class:`ember_code.backend.team_wiring.TeamWiring`::

    bridge = QueueBridge(queue=q)
    bridge.register_on(team)

For finer-grained control the injector / persister are still exposed
as public attributes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .buffer import InjectedRunBuffer
from .injector import QueueInjectorHook
from .persister import QueuePersisterHook
from .schemas import QueueCallbacks


class QueueBridge:
    """Wire a message queue into a live agent as tool + post hooks.

    Owns:

    * one :class:`InjectedRunBuffer` shared between the injector and
      the persister (single source of truth for drained-this-run
      state);
    * one :class:`QueueInjectorHook` (register on ``tool_hooks``);
    * one :class:`QueuePersisterHook` (register on ``post_hooks``).
    """

    def __init__(
        self,
        queue: list[str],
        on_inject: Callable[[str], None] | None = None,
        on_queue_changed: Callable[[], None] | None = None,
    ) -> None:
        self._buffer = InjectedRunBuffer()
        callbacks = QueueCallbacks(
            on_inject=on_inject,
            on_queue_changed=on_queue_changed,
        )
        self.injector = QueueInjectorHook(
            buffer=self._buffer,
            queue=queue,
            callbacks=callbacks,
        )
        self.persister = QueuePersisterHook(buffer=self._buffer)

    @property
    def buffer(self) -> InjectedRunBuffer:
        """Read-only accessor to the shared buffer — useful for
        tests / telemetry that want to peek at drained items without
        going through either hook."""
        return self._buffer

    def register_on(self, team: Any) -> None:
        """Append this bridge's hooks onto a team's ``tool_hooks`` /
        ``post_hooks`` lists.

        ``team`` is left as ``Any`` because the Agno ``Team`` type is
        not part of our public contract — anything with mutable
        ``tool_hooks`` / ``post_hooks`` list attributes will do.
        """
        existing_tool_hooks = team.tool_hooks or []
        team.tool_hooks = [*existing_tool_hooks, self.injector]
        existing_post_hooks = team.post_hooks or []
        team.post_hooks = [*existing_post_hooks, self.persister]


__all__ = ["QueueBridge"]
