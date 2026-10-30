"""What a spawned agent's run hangs from, and how long it may take.

Two pieces of context a ``spawn_agent`` call carries down to the agent it
runs, in :mod:`contextvars` so the stream handler and the approval
coordinator see them without every signature between growing a
parameter:

* :class:`ParentCall` — the call that is waiting for the spawned agent:
  which run made it, under which tool call id, and that run's messages
  so far. When the spawned agent pauses for an approval, the coordinator
  stores the chain of them with the approval
  (:class:`~ember_code.core.approvals.journal.ApprovalJournal`).
  If the backend is gone by the time the person answers, that is what
  lets a new one continue the spawned agent, hand its answer to the
  waiting call as the tool's result, and continue the run that made it
  — the same path on the desktop after quitting and restarting igni.
* :class:`SpawnDeadline` — a spawn's timeout that stops while the person
  is being asked. Approvals have no expiry: an agent waiting on a person
  waits as long as the person takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class ParentCall(BaseModel):
    """A tool call waiting for a spawned agent's answer."""

    #: The agent that made the call: the main agent's name, or a pool agent's.
    agent_name: str
    #: Its Agno id — what Agno files the run under in the session.
    agent_id: str = ""
    run_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: The run's messages up to and including the assistant message that
    #: made the call, as Agno serialises them (``Message.to_dict``).
    messages: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_run_context(
        cls,
        run_context: Any,
        *,
        agent: Any,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> ParentCall | None:
        """The call being made now, from the run context Agno hands a tool.

        The tool call id is the newest assistant tool call to
        ``tool_name`` with these arguments. ``None`` when Agno gave no
        run context, or the call is not in it.
        """
        if run_context is None or agent is None:
            return None
        messages = list(getattr(run_context, "messages", None) or [])
        call_id = cls.find_call(messages, tool_name, tool_args)
        if call_id is None:
            return None
        return cls(
            agent_name=str(getattr(agent, "name", "") or ""),
            agent_id=str(getattr(agent, "id", "") or ""),
            run_id=str(run_context.run_id),
            session_id=str(run_context.session_id),
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            messages=[m.to_dict() for m in messages],
        )

    def turn(self) -> list[dict[str, Any]]:
        """The tool calls of the assistant turn that made this call, in order."""
        for message in reversed(self.messages):
            calls = message.get("tool_calls") or []
            if message.get("role") == "assistant" and any(
                c.get("id") == self.tool_call_id for c in calls
            ):
                return list(calls)
        return []

    @staticmethod
    def find_call(messages: list[Any], tool_name: str, tool_args: dict[str, Any]) -> str | None:
        """The id of the newest assistant tool call to ``tool_name`` with these arguments."""
        for message in reversed(messages):
            if getattr(message, "role", None) != "assistant":
                continue
            for call in getattr(message, "tool_calls", None) or []:
                function = call.get("function") or {}
                if function.get("name") != tool_name:
                    continue
                raw = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (TypeError, ValueError):
                    continue
                if all(arguments.get(k) == v for k, v in tool_args.items() if v not in ("", None)):
                    return str(call.get("id") or "")
        return None


class SpawnCalls:
    """The chain of calls waiting above the agent running now, nearest first."""

    _chain: ClassVar[ContextVar[tuple[ParentCall, ...]]] = ContextVar(
        "igni_spawn_calls", default=()
    )

    @classmethod
    def current(cls) -> list[ParentCall]:
        return list(cls._chain.get())

    @classmethod
    def path_to(cls, name: str, chain: list[ParentCall] | None = None) -> list[str]:
        """The agents from below the main agent down to ``name`` — how a
        dialog names who is asking. The outermost call is the main
        agent's, which the path leaves out."""
        calls = cls.current() if chain is None else chain
        return [*(p.agent_name for p in reversed(calls[:-1])), name]

    @classmethod
    @contextlib.contextmanager
    def entered(cls, call: ParentCall | None) -> Iterator[None]:
        """Run the spawned agent under ``call`` (nothing when ``None``)."""
        if call is None:
            yield
            return
        token = cls._chain.set((call, *cls._chain.get()))
        try:
            yield
        finally:
            cls._chain.reset(token)

    @classmethod
    @contextlib.contextmanager
    def restored(cls, chain: list[ParentCall]) -> Iterator[None]:
        """Run a continued agent under the chain stored with its approval."""
        token = cls._chain.set(tuple(chain))
        try:
            yield
        finally:
            cls._chain.reset(token)


class SpawnDeadline:
    """A spawn's timeout, stopped while the agent waits on a person.

    :meth:`running` bounds a spawn by ``orchestration.sub_team_timeout``
    of its own work; :meth:`waiting_on_person` takes the time a person
    spends deciding out of every enclosing spawn's budget — a nested
    spawn's wait pauses its parents' clocks too.
    """

    _active: ClassVar[ContextVar[tuple[SpawnDeadline, ...]]] = ContextVar(
        "igni_spawn_deadlines", default=()
    )

    def __init__(self, timeout: asyncio.Timeout) -> None:
        self._timeout = timeout
        self._remaining: float | None = None

    @classmethod
    @contextlib.asynccontextmanager
    async def running(cls, seconds: float) -> AsyncIterator[None]:
        async with asyncio.timeout(seconds) as timeout:
            token = cls._active.set((*cls._active.get(), cls(timeout)))
            try:
                yield
            finally:
                cls._active.reset(token)

    def _stop(self) -> None:
        when = self._timeout.when()
        if when is None:
            return
        self._remaining = when - asyncio.get_running_loop().time()
        self._timeout.reschedule(None)

    def _restart(self) -> None:
        if self._remaining is None:
            return
        loop = asyncio.get_running_loop()
        self._timeout.reschedule(loop.time() + max(self._remaining, 0.0))
        self._remaining = None

    @classmethod
    @contextlib.contextmanager
    def waiting_on_person(cls) -> Iterator[None]:
        deadlines = cls._active.get()
        for deadline in deadlines:
            deadline._stop()
        try:
            yield
        finally:
            for deadline in deadlines:
                deadline._restart()


__all__ = ["ParentCall", "SpawnCalls", "SpawnDeadline"]
