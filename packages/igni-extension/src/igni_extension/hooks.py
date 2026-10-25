"""Hooks: the extension answering igni's hook events.

igni's hook events (igni's `docs/HOOKS.md`) are where anyone outside the
agent gets a say — a settings file's command hook, and an extension's
process:

    from igni_extension import Allow, Hooks, Modify, Reject
    from igni_extension.hooks import PlanSubmit

    @ext.hook(Hooks.PLAN_SUBMIT)
    async def grounded(plan: PlanSubmit) -> Allow | Reject:
        if "src/" not in plan.plan:
            return Reject("Name the files the plan changes.")
        return Allow()

Five events **decide** something: `PRE_TOOL_USE`, `POST_TOOL_USE`,
`USER_PROMPT_SUBMIT`, `STOP` and `PLAN_SUBMIT`. The handler is handed the
event's value and answers `Allow()`, `Reject(reason)`, or — where the
event has something to change — `Modify(value)`, the value with only the
fields it may change changed. Returning `None` allows. Every other event
is **told**: the handler hears it, and whatever it returns is not read.

igni asks only about the events in the manifest's `permissions.hooks`,
in the super admin's priority order of extensions, and gives every
answer a deadline that igni sets. A handler that raises, or is too slow,
has not answered: a tool call (`PRE_TOOL_USE`) or a message
(`USER_PROMPT_SUBMIT`) then does not go ahead, and every other event
does.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

from igni_extension.threads import OffTheLoop

logger = logging.getLogger(__name__)


# ── What a handler is handed ──────────────────────────────────────────


class HookValue(BaseModel):
    """What a deciding event hands the handler. The values are the real ones, never clipped."""

    model_config = ConfigDict(extra="allow")

    session_id: str = ""


class PreToolUse(HookValue):
    """A tool the agent is about to run, before igni's own checks."""

    tool_name: str
    #: Its arguments. A change is what igni's checks, and the tool, then see.
    tool_args: dict[str, Any] = Field(default_factory=dict)


class PostToolUse(HookValue):
    """A tool that ran, before the model reads what it returned."""

    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: What it returned, as text. A change is what the model reads; a
    #: reject withholds it.
    result: str


class UserPromptSubmit(HookValue):
    """A message about to start a run."""

    #: What the agent will be sent. A change is what it is sent.
    message: str


class Stop(HookValue):
    """The agent wants to end its turn."""

    #: Its answer, when the place it stops has one; empty otherwise.
    response: str = ""


class PlanSubmit(HookValue):
    """A plan the agent is about to put to the person (`exit_plan_mode`)."""

    #: The plan, in markdown. A change is what the person sees.
    plan: str
    tasks: list[Any] = Field(default_factory=list)
    #: Plans rejected since the agent entered plan mode: 0 for the first.
    attempt: int = 0


class Told(BaseModel):
    """An event that decides nothing, as igni's settings hooks receive it (`docs/HOOKS.md`)."""

    model_config = ConfigDict(extra="allow")

    session_id: str = ""


class HookEvent[T: BaseModel]:
    """One of igni's hook events, and the value a handler is handed."""

    def __init__(
        self, name: str, value: type[T], *, decides: bool, modify: tuple[str, ...] = ()
    ) -> None:
        self.name = name
        self.value = value
        #: Whether the handler's answer counts; `False` for an event it is only told.
        self.decides = decides
        #: The fields of the value a `Modify` may change.
        self.modify = modify

    def parse(self, raw: Any) -> T:
        return self.value.model_validate(raw)

    def __repr__(self) -> str:
        return f"HookEvent({self.name!r})"


class Hooks:
    """Every hook event an extension can answer: `@ext.hook(Hooks.PRE_TOOL_USE)`."""

    PRE_TOOL_USE = HookEvent("PreToolUse", PreToolUse, decides=True, modify=("tool_args",))
    POST_TOOL_USE = HookEvent("PostToolUse", PostToolUse, decides=True, modify=("result",))
    USER_PROMPT_SUBMIT = HookEvent(
        "UserPromptSubmit", UserPromptSubmit, decides=True, modify=("message",)
    )
    STOP = HookEvent("Stop", Stop, decides=True)
    PLAN_SUBMIT = HookEvent("PlanSubmit", PlanSubmit, decides=True, modify=("plan",))

    POST_TOOL_USE_FAILURE = HookEvent("PostToolUseFailure", Told, decides=False)
    SESSION_START = HookEvent("SessionStart", Told, decides=False)
    SESSION_END = HookEvent("SessionEnd", Told, decides=False)
    SUBAGENT_START = HookEvent("SubagentStart", Told, decides=False)
    SUBAGENT_STOP = HookEvent("SubagentStop", Told, decides=False)
    NOTIFICATION = HookEvent("Notification", Told, decides=False)
    PRE_COMPACT = HookEvent("PreCompact", Told, decides=False)
    POST_COMPACT = HookEvent("PostCompact", Told, decides=False)
    INSTRUCTIONS_LOADED = HookEvent("InstructionsLoaded", Told, decides=False)
    TASK_CREATED = HookEvent("TaskCreated", Told, decides=False)
    TASK_COMPLETED = HookEvent("TaskCompleted", Told, decides=False)
    STOP_FAILURE = HookEvent("StopFailure", Told, decides=False)
    PERMISSION_REQUEST = HookEvent("PermissionRequest", Told, decides=False)
    PERMISSION_DENIED = HookEvent("PermissionDenied", Told, decides=False)

    @classmethod
    def all(cls) -> list[HookEvent[Any]]:
        return [value for value in vars(cls).values() if isinstance(value, HookEvent)]


# ── What a handler answers ────────────────────────────────────────────


class Allow(BaseModel):
    """Let it happen as it is."""

    outcome: Literal["allow"] = "allow"


class Reject(BaseModel):
    """Stop it. `reason` reaches the agent or the person, so write it for them."""

    outcome: Literal["reject"] = "reject"
    reason: str

    def __init__(self, reason: str, **data: Any) -> None:
        super().__init__(reason=reason, **data)


class Modify(BaseModel):
    """Let it happen, changed: the whole value, with only the fields it may change changed."""

    outcome: Literal["modify"] = "modify"
    value: Any

    def __init__(self, value: Any, **data: Any) -> None:
        super().__init__(value=value, **data)


type Answer = Allow | Reject | Modify
#: `async def`, or a plain `def` the SDK runs in a worker thread.
type HookHandler[T] = Callable[[T], Awaitable[Answer | None] | Answer | None]


class HookHandlers:
    """One handler per event, and the answers they give."""

    def __init__(self) -> None:
        self._handlers: dict[str, tuple[HookEvent[Any], HookHandler[Any]]] = {}

    def on[T: BaseModel](self, event: HookEvent[T]) -> Callable[[HookHandler[T]], HookHandler[T]]:
        def register(handler: HookHandler[T]) -> HookHandler[T]:
            if event.name in self._handlers:
                raise ValueError(f"{event.name} is hooked twice; one handler answers for it")
            self._handlers[event.name] = (event, handler)
            return handler

        return register

    def events(self) -> list[str]:
        return sorted(self._handlers)

    async def tell(self, name: str, raw: Any) -> None:
        """An event that decides nothing: the handler hears it, and nothing is sent back."""
        found = self._handlers.get(name)
        if found is None:
            return
        event, handler = found
        try:
            await OffTheLoop.call(handler, event.parse(raw))
        except Exception:
            logger.exception("igni: the handler of %s failed", name)

    async def answer(self, hook_id: str, name: str, raw: Any) -> dict[str, Any]:
        """The `answer` request's parameters for one deciding event."""
        found = self._handlers.get(name)
        if found is None:
            logger.warning(
                "igni: asked about %s, which nothing here hooks; allowed. "
                "Add a handler, or take it out of permissions.hooks",
                name,
            )
            return {"hook": hook_id, "outcome": "allow"}
        event, handler = found
        try:
            answer = await OffTheLoop.call(handler, event.parse(raw))
        except Exception as exc:
            logger.exception("igni: the handler of %s failed", name)
            return {"hook": hook_id, "outcome": "failed", "reason": str(exc)}
        if answer is None:
            answer = Allow()
        body: dict[str, Any] = {"hook": hook_id, "outcome": answer.outcome}
        if isinstance(answer, Reject):
            body["reason"] = answer.reason
        elif isinstance(answer, Modify):
            body["value"] = to_jsonable_python(answer.value)
        return body


__all__ = [
    "Allow",
    "Answer",
    "HookEvent",
    "HookHandler",
    "HookHandlers",
    "HookValue",
    "Hooks",
    "Modify",
    "PlanSubmit",
    "PostToolUse",
    "PreToolUse",
    "Reject",
    "Stop",
    "Told",
    "UserPromptSubmit",
]
