"""Abstract base for the two Agno-stream event pumps.

Owns everything that :class:`SubAgentStreamHandler` and
:class:`TeamStreamHandler` share:

* The outer ``while stream is not None`` loop that handles
  pause/resume follow-up streams.
* ``run_id`` / ``session_id`` latching + cancellation-registry
  register/discard around the loop.
* Post-loop belt-and-suspenders ``agent_completed`` emission and
  DB-fallback ``aget_run_output`` finalization.
* One-line ``_emit`` / ``_log_line`` helpers so subclasses read
  clean.

Subclasses provide the ``_handle`` polymorphism — one method per
Agno event type via ``match`` dispatch — and override the small
post-loop hook that shapes the final response string.

Replaces the two 400-line procedural generators in
``orchestrate_streaming.py`` with a class hierarchy.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Generic, TypeVar

from ember_code.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    EventAppender,
    FinalizeResult,
    HitlCoordinatorProtocol,
    OnProgress,
    SubAgentRegistry,
    _EventBase,
)

_stream_log = logging.getLogger("ember_code.llm_calls")

_TState = TypeVar("_TState")


class BaseStreamHandler(Generic[_TState]):
    """Abstract driver for one Agno agent-or-team stream.

    Subclasses supply the state model and the per-event handling
    (``_handle`` returns a follow-up async iterator on
    pause+resume, else ``None``). Everything else — the outer
    loop, cancellation registry hook-up, DB fallback, emit helper
    — lives here so the two concrete subclasses stay focused on
    event dispatch.
    """

    #: The Agno agent or team we're driving. Duck-typed to keep the
    #: base compatible with both ``Agent`` and ``Team`` instances.
    _runnable: Any
    #: The task string passed to ``arun``.
    _task: str
    #: State object owned by this handler; subclasses set it in
    #: ``__init__`` before calling ``super().__init__``.
    state: _TState

    def __init__(
        self,
        runnable: Any,
        task: str,
        *,
        on_progress: OnProgress | None,
        hitl_coordinator: HitlCoordinatorProtocol | None,
        agent_path: list[str] | None,
        card_id: str,
        subagent_registry: SubAgentRegistry,
        event_appender: EventAppender | None,
    ) -> None:
        self._runnable = runnable
        self._task = task
        self._on_progress = on_progress
        self._hitl_coordinator = hitl_coordinator
        self._path: list[str] = list(agent_path or [])
        self._card_id = card_id
        self._registry = subagent_registry
        self._event_appender = event_appender

    # ── Subclass hooks ─────────────────────────────────────────────
    async def _handle(self, event: Any) -> Any:
        """Dispatch one Agno event. Returns a follow-up async
        iterator when a resume was requested (pause+continue),
        else ``None``.

        Concrete subclasses override.
        """
        raise NotImplementedError

    def _shape_final(self, final: str) -> str:
        """Post-loop transformation of the raw final answer string.

        Default: return unchanged. :class:`SubAgentStreamHandler`
        overrides so visualizer sub-agents return a short summary
        line instead of the raw spec JSON.
        """
        return final

    def _agent_completed_path(self) -> str:
        """The ``agent_path`` field on the belt-and-suspenders
        ``agent_completed`` payload. Subclasses override for teams —
        the sub-agent path here isn't correct for the team's own
        finalization emit."""
        return self._path[0] if self._path else "root"

    # ── Public entry point ─────────────────────────────────────────
    async def run(self) -> tuple[str, list[str]]:
        """Drive the stream to completion. Returns ``(response, log)``
        — same shape both concrete handlers exposed before the OOP
        refactor, so callers in ``orchestrate.py`` and the test suite
        pick up the new implementation transparently.
        """
        try:
            stream = self._runnable.arun(self._task, stream=True)
            while stream is not None:
                next_stream = None
                async for event in stream:
                    follow_up = await self._handle(event)
                    if follow_up is not None:
                        next_stream = follow_up
                        break
                stream = next_stream
        finally:
            run_id = getattr(self.state, "current_run_id", None)
            if run_id:
                self._registry.discard(run_id)

        self._ensure_agent_completed_emitted()
        final = await self._fetch_final_content_with_fallback()
        return self._shape_final(final), self._log()

    # ── Shared helpers ─────────────────────────────────────────────
    def _log(self) -> list[str]:
        return self.state.log

    def _log_line(self, line: str) -> None:
        """Append one line to the parent-recap activity log. Not
        FE-facing — the FE gets structured events via ``_emit``."""
        self._log().append(line)

    def _emit(self, event: _EventBase | dict[str, Any]) -> None:
        """Deliver a structured event to the FE.

        Accepts either a :class:`_EventBase` (preferred — new call
        sites use typed payloads) or a raw dict (test-friendly and
        the shape :class:`VisualizationDeltaEvent` already dumps
        into). Stamps ``card_id`` at the boundary so every subclass
        emit stays clean.
        """
        if self._on_progress is None:
            return
        if isinstance(event, _EventBase):
            payload = event.model_dump(by_alias=True, exclude_none=True)
        else:
            payload = dict(event)
        if self._card_id:
            payload["card_id"] = self._card_id
        with contextlib.suppress(Exception):
            self._on_progress(payload)

    def _latch_run_ids(self, event: Any) -> None:
        """Delegate to the state model's latching logic and register
        with the cancellation registry when a fresh run_id lands."""
        newly_run = self.state.latch_ids(event)  # type: ignore[attr-defined]
        if newly_run:
            run_id = self.state.current_run_id  # type: ignore[attr-defined]
            if run_id:
                # ``BackendServer.cancel_run`` iterates the registry
                # to reach every in-flight sub-agent. Without this
                # add, a stuck specialist ignores the top-level ESC.
                self._registry.register(run_id)

    def _ensure_agent_completed_emitted(self) -> None:
        """Belt-and-suspenders ``agent_completed`` emit.

        Agno's specialist ``arun`` doesn't yield ``RunCompletedEvent``
        unless ``stream_events=True`` (which we deliberately keep off
        to avoid noisy lifecycle events on the wire). Without an
        ``agent_completed`` emit, the FE's team-progress card keeps
        spinning after the sub-agent has actually finished — the
        exact bug the user reported at iter 11. Fire our own here
        IF the in-stream handler didn't already (i.e. the caller
        opted into stream_events and a real RunCompletedEvent
        arrived, or the team's per-member handler already emitted).

        Only the sub-agent handler carries an
        ``agent_completed_emitted`` flag; team streams don't emit a
        blanket completion (each member does its own), so this
        hook is a no-op there.
        """
        emitted = getattr(self.state, "agent_completed_emitted", None)
        if emitted is None or emitted:
            return
        # Metrics unknown without RunCompletedEvent — the DB-backed
        # ``aget_run_output`` fallback below can fill in the
        # ``content`` but not the per-agent token totals. FE keeps
        # any previously-known numbers.
        self._emit(AgentCompletedEvent(agent_path=self._agent_completed_path()))
        with contextlib.suppress(Exception):
            self.state.agent_completed_emitted = True  # type: ignore[attr-defined]

    async def _fetch_final_content_with_fallback(self) -> str:
        """Read the final answer back from Agno's session DB.

        ``Agent`` / ``Team`` do not expose a ``run_response`` attribute
        (we tried — it errors with AttributeError); the supported
        way to fetch the canonical ``RunOutput`` after a streaming
        run completes is ``aget_run_output(run_id, session_id)``.

        Fall through to the streamed ``RunCompletedEvent.content``
        (captured in ``state.completed_content``) if the DB lookup
        comes up empty — in practice we've seen the DB-backed lookup
        return ``None`` for MiniMax-driven specialists even though
        the run completed cleanly.
        """
        result = await self._fetch_from_db()
        content = result.content
        if not content and self.state.completed_content:  # type: ignore[attr-defined]
            content = self.state.completed_content  # type: ignore[attr-defined]
            _stream_log.info(
                "stream_handler: used RunCompletedEvent fallback path=%s len=%d",
                self._path,
                len(content),
            )
        return self._clean_final(content)

    async def _fetch_from_db(self) -> FinalizeResult:
        """Isolated DB-backed lookup — narrowed exception surface.

        Only ``AttributeError`` / ``KeyError`` / ``TimeoutError`` (the
        expected "DB not flushed yet" shapes we've seen in the field)
        are swallowed; anything else re-raises so unexpected bugs
        aren't silently hidden.
        """
        run_id = self.state.current_run_id  # type: ignore[attr-defined]
        session_id = self.state.current_session_id  # type: ignore[attr-defined]
        _stream_log.info(
            "stream_handler: stream ended path=%s run_id=%s session_id=%s completed_content_len=%d",
            self._path,
            run_id,
            session_id,
            len(getattr(self.state, "completed_content", "") or ""),
        )
        try:
            run_output = None
            if run_id and session_id:
                run_output = await self._runnable.aget_run_output(
                    run_id=run_id, session_id=session_id
                )
            if run_output is None and session_id:
                run_output = await self._runnable.aget_last_run_output(session_id=session_id)
        except (AttributeError, KeyError, TimeoutError) as exc:
            _stream_log.info(
                "stream_handler: expected read failure path=%s err=%s",
                self._path,
                exc,
            )
            return FinalizeResult(error=str(exc))

        rr_content = getattr(run_output, "content", None) if run_output else None
        rr_status = getattr(run_output, "status", None) if run_output else None
        _stream_log.info(
            "stream_handler: db lookup path=%s found=%s status=%s content_len=%d",
            self._path,
            run_output is not None,
            rr_status,
            len(str(rr_content)) if rr_content else 0,
        )
        return FinalizeResult(
            content=str(rr_content) if rr_content else None,
            # Agno RunOutput.status is a StrEnum in prod and a MagicMock
            # in tests — coerce so Pydantic's string validator accepts
            # both without swallowing the whole DB-fallback path.
            status=str(rr_status) if rr_status is not None else None,
            found=run_output is not None,
        )

    @staticmethod
    def _clean_final(content: str | None) -> str:
        """Strip the ``<think>`` scaffolding and trim.

        Some models leak ``<think>...</think>`` blocks the tokenizer
        template didn't strip — safe to remove verbatim from the
        final response we hand back to the parent agent.
        """
        if not content:
            return ""
        return content.replace("<think>", "").replace("</think>", "").strip()
