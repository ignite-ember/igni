"""Extensions' hooks: igni's hook events, answered by extensions too.

igni's hook events (:class:`~ember_code.core.hooks.events.HookEvent`) are
where anyone outside the agent gets a say — a command or HTTP hook from
the settings files, and an extension's process (``@ext.hook`` in the
``igni-extension`` SDK). This module is the extensions' half, as a seam:
the core does not import extensions. It asks :class:`ExtensionHookGate`,
and the backend installs what answers (the host's
:class:`~ember_code.extensions.host.hooks.ExtensionHooks`). Nothing
installed, nobody objects.

Five events **decide** something, and an extension's answer counts on
them (:data:`DECIDING`): it may let the event go ahead, stop it with a
reason, or — where the event has something to change — change it.
Extensions are asked first, one at a time in the super admin's priority
order, each handed what the one before left; then igni's own configured
hooks run on the result, as plugin hooks run before project hooks so the
project keeps the last word. Every other event is **told**: the
extensions that hook it hear it, and nothing waits on them.

**A silence is igni's to decide**, never the extension's: no answer in
time, a process that is not running, an answer the event cannot take.
``CLOSED`` — the event does not happen — for a tool call and for a
message to the model, because nothing should run or reach the model
unchecked by a policy that is meant to check it. ``OPEN`` elsewhere.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.hooks.events import HookEvent


class HookFailMode(StrEnum):
    """What a silence means for an event."""

    #: The event does not happen.
    CLOSED = "closed"
    #: The event happens as if that extension had let it.
    OPEN = "open"


# ── What an extension is handed, per deciding event ──────────────────


class HookValue(BaseModel):
    """What an extension is handed. The values are the real ones, never clipped."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = ""


class PreToolUseValue(HookValue):
    """A tool the agent is about to run, before igni's own checks."""

    tool_name: str
    #: Its arguments. A change is what igni's checks, and the tool, then see.
    tool_args: dict[str, Any] = Field(default_factory=dict)


class PostToolUseValue(HookValue):
    """A tool that ran, before the model reads what it returned."""

    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: What it returned, as text. A change is what the model reads.
    result: str


class UserPromptSubmitValue(HookValue):
    """A message about to start a run."""

    #: What the agent will be sent. A change is what it is sent.
    message: str


class StopValue(HookValue):
    """The agent wants to end its turn."""

    #: Its answer, when the place it stops has one; empty otherwise.
    response: str = ""


class PlanSubmitValue(HookValue):
    """A plan the agent is about to put to the person (``exit_plan_mode``)."""

    #: The plan, in markdown. A change is what the person sees.
    plan: str
    tasks: list[Any] = Field(default_factory=list)
    #: Plans rejected since the agent entered plan mode: 0 for the first.
    attempt: int = 0


class ExtensionHookSpec(BaseModel):
    """One deciding event: what an extension is handed, may change, and what a reject does."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    event: HookEvent
    value: type[HookValue]
    #: The fields of the value a change may touch.
    modify: tuple[str, ...] = ()
    fails: HookFailMode = HookFailMode.OPEN
    #: What a reject does, in a sentence.
    on_reject: str


#: The events an extension's answer decides. Every other event is told.
DECIDING: dict[HookEvent, ExtensionHookSpec] = {
    spec.event: spec
    for spec in (
        ExtensionHookSpec(
            event=HookEvent.PRE_TOOL_USE,
            value=PreToolUseValue,
            modify=("tool_args",),
            fails=HookFailMode.CLOSED,
            on_reject="The tool does not run; the agent is told why, as for igni's own refusals.",
        ),
        ExtensionHookSpec(
            event=HookEvent.POST_TOOL_USE,
            value=PostToolUseValue,
            modify=("result",),
            fails=HookFailMode.OPEN,
            on_reject="The model is not shown the result, only that it was withheld and why.",
        ),
        ExtensionHookSpec(
            event=HookEvent.USER_PROMPT_SUBMIT,
            value=UserPromptSubmitValue,
            modify=("message",),
            fails=HookFailMode.CLOSED,
            on_reject="The run does not start; the reason is shown as an error.",
        ),
        ExtensionHookSpec(
            event=HookEvent.STOP,
            value=StopValue,
            fails=HookFailMode.OPEN,
            on_reject=(
                "As a Stop hook's block: where igni can send the agent back (a turn "
                "run through the session), it revises its answer with the reason, up "
                "to three times; elsewhere the reason is shown after the run."
            ),
        ),
        ExtensionHookSpec(
            event=HookEvent.PLAN_SUBMIT,
            value=PlanSubmitValue,
            modify=("plan",),
            fails=HookFailMode.OPEN,
            on_reject="The plan goes back to the agent with the reason; it may submit again.",
        ),
    )
}


class ExtensionHookVerdict(BaseModel):
    """Whether a deciding event goes ahead, and with what."""

    allowed: bool
    #: The value it goes ahead with, as the last extension left it.
    value: dict[str, Any]
    #: Why not, when it does not.
    reason: str = ""
    #: The extension that stopped it.
    by: str = ""


class ExtensionHookAnswerer(Protocol):
    """Whoever answers for the extensions: the host, in igni's backend."""

    def hooks(self, event: HookEvent) -> bool:
        """Whether any extension hooks ``event`` — a fast no for the common case."""
        ...

    async def ask(self, spec: ExtensionHookSpec, value: dict[str, Any]) -> ExtensionHookVerdict:
        """Ask every extension that hooks a deciding event, in priority order."""
        ...

    def tell(self, event: str, payload: dict[str, Any]) -> None:
        """Tell every extension that hooks an event that decides nothing. Waits on nobody."""
        ...

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        """The agent called a tool; whether its server answered, for the extension it is from."""
        ...


class ExtensionHookGate:
    """The process's one gate. The backend installs what answers it."""

    _answerer: ClassVar[ExtensionHookAnswerer | None] = None

    @classmethod
    def install(cls, answerer: ExtensionHookAnswerer | None) -> None:
        cls._answerer = answerer

    @classmethod
    def hooked(cls, event: HookEvent) -> bool:
        """Whether any extension hooks ``event``."""
        return cls._answerer is not None and cls._answerer.hooks(event)

    @classmethod
    async def ask(cls, value: HookValue, event: HookEvent) -> ExtensionHookVerdict:
        """Ask before a deciding event happens; it goes ahead as it is when nobody hooks it."""
        current = value.model_dump(mode="json")
        spec = DECIDING[event]
        if not isinstance(value, spec.value):
            raise TypeError(f"{event.value} is asked with {spec.value.__name__}")
        if cls._answerer is None or not cls._answerer.hooks(event):
            return ExtensionHookVerdict(allowed=True, value=current)
        return await cls._answerer.ask(spec, current)

    @classmethod
    def tell(cls, event: str, payload: dict[str, Any]) -> None:
        """Tell the extensions that hook an event that decides nothing."""
        if cls._answerer is None:
            return
        try:
            hook_event = HookEvent(event)
        except ValueError:
            return
        if hook_event in DECIDING or not cls._answerer.hooks(hook_event):
            return
        cls._answerer.tell(event, payload)

    @classmethod
    def tool_called(cls, tool_name: str, *, answered: bool, detail: str = "") -> None:
        """Say whether a tool the agent called answered.

        An extension whose tools time out or crash again and again is
        faulty, and the host counts it here; a tool that is not an
        extension's is nobody's business.
        """
        if cls._answerer is not None:
            cls._answerer.tool_called(tool_name, answered=answered, detail=detail)


__all__ = [
    "DECIDING",
    "ExtensionHookAnswerer",
    "ExtensionHookGate",
    "ExtensionHookSpec",
    "ExtensionHookVerdict",
    "HookFailMode",
    "HookValue",
    "PlanSubmitValue",
    "PostToolUseValue",
    "PreToolUseValue",
    "StopValue",
    "UserPromptSubmitValue",
]
