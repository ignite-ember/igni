"""Pydantic schemas for the queue-hook seam.

Each model captures a piece of state or a boundary object that used to
live as loose dicts / callables / string constants in
``core/queue_hook.py``:

* :class:`InjectedMessage` — one queued text with a stable id / timestamp
  (fields available for future telemetry; the persister currently reads
  only ``.text`` — that's intentional).
* :class:`InjectedNote` — the ``[USER MESSAGE WHILE YOU WERE WORKING]``
  wire format, previously an inline ``USER_NOTE_HEADER`` constant plus a
  static ``_augment_result`` method.
* :class:`QueueCallbacks` — bundles the two optional UI callbacks that
  are always threaded together.
* :class:`ToolHookInvocation` / :class:`PostHookInvocation` — typed
  wrappers around the ``**kwargs`` bags Agno hands to tool / post
  hooks.
* :class:`SupportsRunOutput` — Protocol for typing the append site
  without depending on Agno's concrete ``RunOutput``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from agno.models.message import Message
from pydantic import BaseModel, ConfigDict, Field

USER_NOTE_HEADER = "USER MESSAGE WHILE YOU WERE WORKING"


class InjectedMessage(BaseModel):
    """A single queued message that has been drained during a run.

    The ``id`` / ``created_at`` fields are available for future
    telemetry — today the persister writes only ``.text`` to the
    session history. Documented here so the fields don't drift into
    "fake carrying metadata" territory: they exist, they're stable,
    they're just not surfaced yet.
    """

    text: str
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InjectedNote(BaseModel):
    """Wire format for the note that gets appended onto a tool result.

    Owns the ``[USER MESSAGE WHILE YOU WERE WORKING]`` header and the
    branch-per-result-type rendering logic that used to live in
    :meth:`QueueInjectorHook._augment_result`.
    """

    header: str = USER_NOTE_HEADER
    messages: list[InjectedMessage]

    def render_onto(self, result: Any) -> Any:
        """Suffix a tool's output with a clearly-marked user-note block.

        Preserves byte-for-byte the three branches of the original
        ``_augment_result`` static method:

        * ``None`` → note without leading newlines
        * ``str`` → original + note
        * anything else → ``repr(result) + note``
        """
        joined = "\n".join(f"- {m.text}" for m in self.messages)
        note = f"\n\n[{self.header}]\n{joined}\n[END USER MESSAGE]"
        if result is None:
            return note.lstrip("\n")
        if isinstance(result, str):
            return result + note
        return f"{result!r}{note}"


class QueueCallbacks(BaseModel):
    """Optional UI callbacks fired as the queue is drained.

    Bundled together because they're always passed together and both
    optional — keeps :class:`QueueBridge` / :class:`QueueInjectorHook`
    from carrying two loose ``Callable | None`` kwargs.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    on_inject: Callable[[str], None] | None = None
    on_queue_changed: Callable[[], None] | None = None

    def notify_inject(self, text: str) -> None:
        if self.on_inject is not None:
            self.on_inject(text)

    def notify_queue_changed(self) -> None:
        if self.on_queue_changed is not None:
            self.on_queue_changed()


class ToolHookInvocation(BaseModel):
    """Typed wrapper around the kwargs Agno hands a tool_hook.

    Agno's ``_build_hook_args`` recognises specific parameter names
    (``name``, ``func``, ``args``, ``agent``, …). We accept those as
    named fields and hold the remainder in ``extra`` for forward-compat
    if Agno grows new kwargs.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = ""
    func: Callable[..., Any] | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    agent: Any = None
    extra: dict[str, Any] = Field(default_factory=dict)


class PostHookInvocation(BaseModel):
    """Typed wrapper around the kwargs Agno hands a post_hook.

    ``run_output`` is left as ``Any`` because Agno's outermost frame
    hands us an untyped object; the append site is narrowed via
    :class:`SupportsRunOutput` at the point of use.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_output: Any = None
    extra: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class SupportsRunOutput(Protocol):
    """Protocol for the persister's target — narrows Agno's ``Any``.

    Any object with a mutable ``messages: list[Message]`` attribute
    (Agno's ``RunOutput``, our ``SimpleNamespace`` test double,
    plugins that stub the same shape) satisfies this Protocol.
    """

    messages: list[Message] | None


__all__ = [
    "USER_NOTE_HEADER",
    "InjectedMessage",
    "InjectedNote",
    "PostHookInvocation",
    "QueueCallbacks",
    "SupportsRunOutput",
    "ToolHookInvocation",
]
