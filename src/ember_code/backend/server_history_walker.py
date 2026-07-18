"""Per-Agno-run message walker used by
:class:`ember_code.backend.server_history.ChatHistoryRebuilder`.

Owns the mutable per-run counters + the role-dispatch that used
to be an inline for-loop inside the old ``get_chat_history`` free
function. Kept in its own module because ``server_history.py``
would otherwise blow past the 300-LoC guideline; the rebuilder is
already a full-page class on its own.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ember_code.backend.restore_content import AssistantContentRestorer
from ember_code.backend.schemas_history import (
    AgnoRunMessageView,
    AgnoRunMetricsView,
    AssistantTurn,
    ChatTurn,
    PlanTurn,
    StatsTurn,
    ThinkingTurn,
    ToolTurn,
    UserTurn,
)
from ember_code.protocol.agno_tool_formatter import default_registry

logger = logging.getLogger(__name__)


class RunWalker:
    """Per-Agno-run message dispatcher.

    Owns the mutable per-run counters (``input_chars``,
    ``assistant_chars``, ``plan_calls_in_run``) plus the running
    across-runs char totals threaded in from the rebuilder
    (``history_chars``, ``system_chars``). Dispatches each Agno
    message to a role-specific ``_handle_*`` method.

    :meth:`walk` returns the walker's turn list — the caller
    extends its outer accumulator with the result. No shared
    mutable ``out`` field; each walker owns its own emissions.

    ``plan_calls_in_run`` intentionally stays ``dict[str, dict]`` —
    it holds arbitrary parsed ``exit_plan_mode`` tool-argument JSON
    whose schema we don't own.
    """

    def __init__(
        self,
        *,
        run_id: str,
        history_chars: int,
        system_chars: int,
    ) -> None:
        self.run_id = run_id
        self.history_chars = history_chars
        self.system_chars = system_chars
        # Snapshot BEFORE walking this run's messages — the context
        # the model saw on its way into this turn (not yet including
        # this turn's user message).
        self.input_chars = history_chars
        self.assistant_chars = 0
        self.plan_calls_in_run: dict[str, dict[str, Any]] = {}
        self._out: list[ChatTurn] = []

    # ── Public entry points ────────────────────────────────────────

    def walk(self, messages: list[Any]) -> list[ChatTurn]:
        """Validate + dispatch every message; return the collected
        turns. Malformed Agno messages log a debug line and skip —
        one bad message does not abort the whole run's history."""
        for m in messages:
            try:
                view = AgnoRunMessageView.model_validate(m, from_attributes=True)
            except Exception as exc:
                logger.debug("Skipping malformed Agno message: %s", exc)
                continue
            self._dispatch(view)
        return self._out

    def finalize(self, metrics_raw: Any) -> StatsTurn | None:
        """Emit the run's stats badge, or ``None`` for a degenerate
        (all-zero) run.

        Input / output are ALWAYS chars/4 estimates of the model's
        actual prompt — NOT Agno's billed numbers. Agno's
        ``run.metrics.input_tokens`` sums across model iterations
        within a turn, so the same conversation reads non-monotonic.
        The live path corrects this via ``count_context_tokens``
        after each run, but historical runs have no corrected
        number to restore.
        """
        full_input_chars = self.system_chars + self.input_chars
        input_tokens = max(1, full_input_chars // 4) if full_input_chars else 0
        output_tokens = max(1, self.assistant_chars // 4) if self.assistant_chars else 0
        if not (input_tokens or output_tokens):
            return None
        metrics = _coerce_metrics(metrics_raw)
        return StatsTurn(
            run_id=self.run_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=metrics.reasoning_tokens,
            duration=metrics.duration,
        )

    # ── Dispatch ───────────────────────────────────────────────────

    def _dispatch(self, view: AgnoRunMessageView) -> None:
        """Send one message to the role-specific handler.

        ``from_history`` messages are Agno's replay of a prior turn
        we've already emitted — skip. Unknown roles are logged and
        dropped rather than silently rewritten to ``user`` — the
        wire contract requires accurate roles, so refusing to emit
        beats coercing.
        """
        if view.from_history:
            return
        role = view.role
        if role == "system":
            self._handle_system(view)
        elif role == "tool":
            self._handle_tool(view)
        elif role == "assistant":
            self._handle_assistant(view)
        elif role == "user":
            self._handle_user(view)
        else:
            logger.debug(
                "Skipping message with unrecognised role %r (run_id=%s)",
                role,
                self.run_id,
            )

    # ── Role handlers ──────────────────────────────────────────────

    def _handle_system(self, view: AgnoRunMessageView) -> None:
        """System prompt + tool-defs overhead. Captured once and
        added as a constant to every input-token estimate. Not
        rendered as a turn."""
        if not self.system_chars:
            self.system_chars = len(view.content_str)

    def _handle_tool(self, view: AgnoRunMessageView) -> None:
        """Tool-result message. Either emit a :class:`ToolTurn` OR,
        if this result correlates with a prior ``exit_plan_mode``
        assistant tool-call, a :class:`PlanTurn` in its place."""
        content = view.content_str
        if view.tool_call_id and view.tool_call_id in self.plan_calls_in_run:
            plan_args = self.plan_calls_in_run.pop(view.tool_call_id)
            plan_text = str(plan_args.get("plan", "")).strip()
            if plan_text:
                # ``state`` is filled in by ``_fill_plan_states``
                # post-walk; empty here so the pass can tell "we
                # set it" from "never touched".
                self._out.append(
                    PlanTurn(
                        plan=plan_text,
                        tasks=list(plan_args.get("tasks") or []),
                        state="",
                        run_id=self.run_id,
                        created_at=view.created_at,
                    )
                )
                self.history_chars += len(content)
                return

        args_summary = (
            ""
            if view.tool_args is None
            else AssistantContentRestorer.format_tool_args(view.tool_args)
        )
        self._out.append(
            ToolTurn(
                tool_name=view.tool_name,
                friendly_name=default_registry().friendly_name(view.tool_name),
                args=args_summary,
                content=content,
                is_error=view.tool_call_error,
                run_id=self.run_id,
                created_at=view.created_at,
            )
        )
        self.history_chars += len(content)

    def _handle_assistant(self, view: AgnoRunMessageView) -> None:
        """Assistant message — two thinking sources plus the visible
        reply, interleaved so the restored chat reads in the same
        order the live stream produced.

        Source 1: Agno's ``reasoning_content`` field (Anthropic
        sidecar reasoning) — one thinking block BEFORE the reply.
        Source 2: inline ``<think>...</think>`` tags inside the
        content itself (MiniMax-style) — split and interleaved.

        Also stashes any ``exit_plan_mode`` tool calls keyed by
        call_id so the later tool-result message rewrites into a
        PlanCard turn."""
        content = view.content_str
        reasoning = view.reasoning_content
        if reasoning and reasoning.strip():
            self._out.append(
                ThinkingTurn(
                    content=reasoning,
                    run_id=self.run_id,
                    created_at=view.created_at,
                )
            )
        self._stash_exit_plan_calls(view.tool_calls)
        for part_role, part_text in AssistantContentRestorer.split_content(content):
            if part_role == "assistant":
                self._out.append(
                    AssistantTurn(
                        content=part_text,
                        run_id=self.run_id,
                        created_at=view.created_at,
                    )
                )
                self.assistant_chars += len(part_text)
            else:
                self._out.append(
                    ThinkingTurn(
                        content=part_text,
                        run_id=self.run_id,
                        created_at=view.created_at,
                    )
                )
        # Count the full original content toward history — that's
        # what the model actually saw on the next turn (including
        # the <think> tags).
        self.history_chars += len(content)

    def _handle_user(self, view: AgnoRunMessageView) -> None:
        """User turn. Display AND count. The user message of this
        run lands in the model's input but not in the pre-run
        snapshot, so it adds to :attr:`input_chars`."""
        content = view.content_str
        self._out.append(
            UserTurn(
                content=content,
                run_id=self.run_id,
                created_at=view.created_at,
            )
        )
        self.history_chars += len(content)
        self.input_chars += len(content)

    # ── Private helpers ────────────────────────────────────────────

    def _stash_exit_plan_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Scan the assistant message's ``tool_calls`` for
        ``exit_plan_mode`` invocations and stash their parsed args
        by ``call_id`` for later correlation."""
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if fn.get("name") != "exit_plan_mode":
                continue
            args_raw = fn.get("arguments")
            if isinstance(args_raw, str):
                try:
                    parsed = json.loads(args_raw)
                except json.JSONDecodeError:
                    continue
            elif isinstance(args_raw, dict):
                parsed = args_raw
            else:
                continue
            call_id = str(tc.get("id") or "")
            if call_id:
                self.plan_calls_in_run[call_id] = parsed


def _coerce_metrics(metrics_raw: Any) -> AgnoRunMetricsView:
    """Wrap a raw Agno metrics struct (or ``None``) in the typed
    view. Missing metrics collapse to a zero-filled view so the
    stats-badge always has real fields to read."""
    if metrics_raw is None:
        return AgnoRunMetricsView()
    try:
        return AgnoRunMetricsView.model_validate(metrics_raw, from_attributes=True)
    except Exception as exc:
        logger.debug("Malformed Agno metrics; using zero defaults: %s", exc)
        return AgnoRunMetricsView()


__all__ = ["RunWalker"]
