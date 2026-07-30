"""Sub-agent stream handler — one specialist agent, one Agno stream.

Concrete subclass of :class:`BaseStreamHandler` for the ``spawn_agent``
path. The base class owns the outer loop + cancellation registry +
DB fallback; this class owns per-event dispatch via ``match``.

Replaces the ~230-line ``_handle`` closure at the top of the old
``orchestrate_streaming.py``. Each Agno event type has a dedicated
method — polymorphism instead of an ``isinstance`` ladder — which
also makes it easy to add a new event type without hunting a giant
``elif`` chain.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from agno.run import agent as agent_events

from ember_code.core.tools.orchestrate_events import (
    AgentCompletedEvent,
    AgentPausedEvent,
    ContentPreviewEvent,
    EventAppender,
    HitlCoordinatorProtocol,
    LogSymbols,
    OnProgress,
    RunErrorEvent,
    SubAgentRegistry,
    ToolCompletedEvent,
    ToolStartedEvent,
    VisualizationDeltaEvent,
)
from ember_code.core.tools.orchestrate_preview import PREVIEWS
from ember_code.core.tools.orchestrate_stream_handler import BaseStreamHandler
from ember_code.core.tools.subagent_stream import SubAgentStreamState

_stream_log = logging.getLogger("ember_code.llm_calls")


class SubAgentStreamHandler(BaseStreamHandler[SubAgentStreamState]):
    """Drive one Agno agent stream to completion.

    Constructor mirrors the old ``run_agent_streaming`` function
    signature — the module-level wrapper in
    ``orchestrate_streaming.py`` builds one of these and calls
    ``.run()``.
    """

    def __init__(
        self,
        agent: Any,
        task: str,
        *,
        on_progress: OnProgress | None = None,
        hitl_coordinator: HitlCoordinatorProtocol | None = None,
        agent_path: list[str] | None = None,
        card_id: str = "",
        subagent_registry: SubAgentRegistry,
        event_appender: EventAppender | None = None,
    ) -> None:
        super().__init__(
            agent,
            task,
            on_progress=on_progress,
            hitl_coordinator=hitl_coordinator,
            agent_path=agent_path,
            card_id=card_id,
            subagent_registry=subagent_registry,
            event_appender=event_appender,
        )
        path = list(agent_path or [])
        self.state = SubAgentStreamState(
            agent_path_id=".".join(path) if path else "root",
            card_id=card_id,
        )

    # ── Base-class hooks ───────────────────────────────────────────
    def _agent_completed_path(self) -> str:
        return self.state.agent_path_id

    def _shape_final(self, final: str) -> str:
        # For the visualizer sub-agent, the tool call already streamed
        # progressive ``visualization_delta`` events to the FE (see
        # ``_on_custom_event`` below), so the card is already
        # rendered. Return a short summary so the PARENT agent's
        # context doesn't get polluted with the raw spec JSON — the
        # parent just quotes the summary to the user.
        if self._path and self._path[-1] == "visualizer":
            return "Emitted visualization to the client."
        return final

    # ── Dispatch ───────────────────────────────────────────────────
    async def _handle(self, event: Any) -> Any:
        """Drive progress reporting and HITL bridging.

        We don't try to reassemble the model's text from streaming
        events — Agno already accumulates that into
        ``agent.run_response.content`` when the run ends. We just
        watch the stream for tool-call lifecycle (for activity log
        lines), HITL pauses (for the bridge), and content deltas
        (for the live progress preview).

        Returns a follow-up async iterator when we resumed the run
        after a pause; otherwise ``None``.
        """
        # Latch identifiers off every event that carries them —
        # RunStartedEvent-only capture misses the specialist path,
        # see :meth:`SubAgentStreamState.latch_ids`.
        self._latch_run_ids(event)

        if isinstance(event, agent_events.RunStartedEvent):
            return None
        if isinstance(event, agent_events.RunPausedEvent):
            return await self._on_run_paused(event)
        if isinstance(event, agent_events.ToolCallStartedEvent):
            await self._on_tool_started(event)
            return None
        if isinstance(event, agent_events.ToolCallCompletedEvent):
            self._on_tool_completed(event)
            return None
        if isinstance(event, agent_events.ToolCallErrorEvent):
            self._on_tool_error(event)
            return None
        if isinstance(event, agent_events.RunErrorEvent):
            self._on_run_error(event)
            return None
        if isinstance(event, agent_events.RunCompletedEvent):
            self._on_run_completed(event)
            return None
        if isinstance(event, agent_events.CustomEvent):
            self._on_custom_event(event)
            return None
        if isinstance(event, agent_events.RunContentEvent):
            self._on_run_content(event)
            return None
        return None

    # ── Per-event handlers ─────────────────────────────────────────
    async def _on_run_paused(self, event: Any) -> Any:
        state = self.state
        reqs = getattr(event, "active_requirements", None) or []
        if self._hitl_coordinator is None or not reqs:
            self._log_line(
                f"  {LogSymbols.T_TRUNK.value}  "
                f"{LogSymbols.WARNING.value} paused: no HITL bridge available"
            )
            self._emit(
                RunErrorEvent(
                    agent_path=state.agent_path_id,
                    error="paused: no HITL bridge available",
                )
            )
            return None
        run_id = getattr(event, "run_id", "") or state.current_run_id or ""
        session_id = getattr(event, "session_id", None) or state.current_session_id
        req_ids: list[str] = []
        for req in reqs:
            req_id = await self._hitl_coordinator.push_requirement(
                req, run_id=run_id, agent_path=self._path
            )
            req_ids.append(req_id)
        # One concise activity line per pause batch — the user has
        # the dialog itself, and the parent agent doesn't need our
        # internal req_ids in its context.
        pause_line = (
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} "
            f"awaiting approval ({len(req_ids)} tools)"
            if len(req_ids) > 1
            else f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.PAUSE.value} awaiting approval"
        )
        self._log_line(pause_line)
        self._emit(
            AgentPausedEvent(
                agent_path=state.agent_path_id,
                count=len(req_ids),
            )
        )
        try:
            for req_id in req_ids:
                await self._hitl_coordinator.wait_resolved(req_id)
        finally:
            for req_id in req_ids:
                self._hitl_coordinator.cleanup(req_id)
        # ``run_id`` + ``session_id`` is enough for Agno to find the
        # paused run — but only when the agent has a ``db=`` so the
        # run was actually persisted. The pool wires ``InMemoryDb``
        # into every specialist for exactly this reason.
        return self._runnable.acontinue_run(
            run_id=run_id,
            session_id=session_id,
            requirements=reqs,
            stream=True,
        )

    async def _on_tool_started(self, event: Any) -> None:
        state = self.state
        te = event.tool
        tn = (te.tool_name or "tool") if te else "tool"
        ta = te.tool_args if te else {}
        args_preview = PREVIEWS.format_args(ta)
        tc_id = getattr(te, "tool_call_id", None) if te else None
        state.current_tool = tn
        if tn == "visualize" and isinstance(ta, dict) and isinstance(ta.get("spec"), dict):
            await self._emit_visualizer_final_delta(ta["spec"])
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_BRANCH.value} {tn}({args_preview})"
        )
        self._emit(
            ToolStartedEvent(
                agent_path=state.agent_path_id,
                tool=tn,
                tool_call_id=tc_id,
                args=args_preview,
            )
        )

    async def _emit_visualizer_final_delta(self, spec: dict) -> None:
        """Visualizer final delta.

        The model finished streaming args, ``ta["spec"]`` now holds
        the complete parsed spec. Emit one closing ``final=True``
        delta so the FE can transition the card out of "streaming"
        mode, and log to the session event log so a reload replays
        it.
        """
        state = self.state
        final_event = VisualizationDeltaEvent(
            agent_path=state.agent_path_id,
            spec_id=state.vis_spec_id,
            spec_json=json.dumps(spec),
            final=True,
        )
        final_payload = final_event.model_dump(by_alias=True)
        self._emit(final_payload)
        if self._event_appender is None:
            return
        with contextlib.suppress(Exception):
            # Use the PARENT (top-level) run_id, not the visualizer
            # sub-agent's own run_id — see
            # ``parent_top_run_id`` docstring on
            # :class:`SubAgentStreamState` and the ``get_chat_history``
            # splicing block in ``backend/server.py``.
            await self._event_appender(
                "visualization_delta",
                final_payload,
                state.parent_top_run_id or state.current_run_id or "",
            )

    def _on_tool_completed(self, event: Any) -> None:
        state = self.state
        te = event.tool
        r = getattr(te, "result", None) if te else None
        tn = (te.tool_name if te else None) or state.current_tool or "tool"
        tc_id = getattr(te, "tool_call_id", None) if te else None
        result_preview = PREVIEWS.format_result(r)
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} {result_preview}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=state.agent_path_id,
                tool=tn,
                tool_call_id=tc_id,
                result=result_preview,
                is_error=False,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    def _on_tool_error(self, event: Any) -> None:
        state = self.state
        te = getattr(event, "tool", None)
        tn = (te.tool_name if te else None) or state.current_tool or "tool"
        tc_id = getattr(te, "tool_call_id", None) if te else None
        err = str(getattr(event, "error", "?"))
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.T_TRUNK.value}  "
            f"{LogSymbols.T_LEAF.value} ERROR: {err[:60]}"
        )
        self._emit(
            ToolCompletedEvent(
                agent_path=state.agent_path_id,
                tool=tn,
                tool_call_id=tc_id,
                result=err[:200],
                is_error=True,
            )
        )
        if state.current_tool == tn:
            state.current_tool = None

    def _on_run_error(self, event: Any) -> None:
        state = self.state
        err = str(getattr(event, "content", "") or getattr(event, "error", "?"))
        self._log_line(
            f"  {LogSymbols.T_TRUNK.value}  {LogSymbols.WARNING.value} RUN ERROR: {err[:200]}"
        )
        self._emit(
            RunErrorEvent(
                agent_path=state.agent_path_id,
                error=err[:400],
            )
        )
        _stream_log.info("subagent_stream: RunErrorEvent path=%s err=%s", self._path, err[:300])

    def _on_run_completed(self, event: Any) -> None:
        # Last RunCompletedEvent of the run carries the full final
        # answer — keep the latest non-empty value as a fallback in
        # case the DB-backed lookup below comes up empty.
        state = self.state
        content = getattr(event, "content", None)
        state.record_completion(content)
        # Emit ``agent_completed`` with the run's metrics so the FE
        # can surface per-agent token totals. Agno's ``event.metrics``
        # is a ``Metrics`` object with the same fields the top-level
        # RunCompleted carries.
        mt = getattr(event, "metrics", None)
        self._emit(
            AgentCompletedEvent(
                agent_path=state.agent_path_id,
                is_error=False,
                input_tokens=int(getattr(mt, "input_tokens", 0) or 0) if mt else 0,
                output_tokens=int(getattr(mt, "output_tokens", 0) or 0) if mt else 0,
                reasoning_tokens=int(getattr(mt, "reasoning_tokens", 0) or 0) if mt else 0,
            )
        )

    def _on_custom_event(self, event: Any) -> None:
        """Tool-arg streaming (see ``_LoggingModel.process_response_stream``).

        When the visualizer sub-agent calls ``visualize({spec: ...})``,
        we get a ``tool_call_input_delta`` event on every model chunk
        with the accumulated arguments JSON. We extract the ``spec``
        sub-field with partial-json and forward as a
        ``visualization_delta`` so the FE renders live.
        """
        if getattr(event, "event", "") != "tool_call_input_delta":
            return
        if getattr(event, "tool_name", "") != "visualize":
            return
        args_partial = str(getattr(event, "arguments_partial", "") or "")
        state = self.state
        if len(args_partial) <= state.vis_last_emitted_len:
            return
        now_s = time.monotonic()
        if not state.can_emit_vis_delta(now_s):
            return
        event_out = VisualizationDeltaEvent.from_partial_args(
            agent_path=state.agent_path_id,
            spec_id=state.vis_spec_id,
            args_partial=args_partial,
        )
        if event_out is None:
            return
        state.record_vis_emission(now_s, len(args_partial))
        self._emit(event_out.model_dump(by_alias=True))

    def _on_run_content(self, event: Any) -> None:
        # Live progress preview only — final content comes from
        # ``agent.run_response.content`` after the stream ends.
        chunk = event.content or ""
        preview = self.state.append_content_delta(chunk)
        if preview is None:
            return
        self._emit(
            ContentPreviewEvent(
                agent_path=self.state.agent_path_id,
                text=preview,
            )
        )
