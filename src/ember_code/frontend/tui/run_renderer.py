"""Protocol-event → widget rendering, extracted from ``run_controller.py``.

The main ``_run`` loop reads protocol messages off the BE
stream; every branch of ``_render`` dispatches to a specific
``_on_*`` handler. All of it — the dispatch + the per-message
handlers — lives here so ``run_controller.py`` stays focused
on the flow orchestration.

Free functions taking ``controller: RunController`` as first
arg (same pattern as the ``server_*``/``tui/*_handlers``
modules).

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from textual.widgets import Static

from ember_code.frontend.tui.widgets import (
    AgentActivityWidget,
    AgentRunContainer,
    StreamingMessageWidget,
    ToolCallLiveWidget,
)
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_events import _build_diff_table

if TYPE_CHECKING:
    from ember_code.frontend.tui.run_controller import RunController

logger = logging.getLogger(__name__)


async def render(controller: "RunController", proto: Any) -> None:
    """Render a protocol message to TUI widgets.

    This function has ZERO Agno imports — it only reads plain
    protocol message fields (str, int, bool).
    """
    if isinstance(proto, msg.ContentDelta):
        if proto.is_thinking:
            await append_thinking(controller, proto.text)
        else:
            await on_content_chunk(controller, proto.text)
            controller._streamed = True
            controller._run_output_text.append(proto.text)

    elif isinstance(proto, msg.ToolStarted):
        await on_tool_started(
            controller,
            proto.friendly_name,
            proto.tool_name,
            proto.args_summary,
            proto.run_id or None,
        )

    elif isinstance(proto, msg.ToolCompleted):
        controller._status.update_status_bar()
        on_tool_completed(
            controller,
            proto.summary,
            proto.full_result,
            proto.run_id or None,
            proto.has_markup,
            proto.diff_rows,
            proto.is_error,
        )

    elif isinstance(proto, msg.ToolError):
        on_tool_error(controller, proto.error)

    elif isinstance(proto, msg.ModelCompleted):
        on_tokens(
            controller,
            proto.input_tokens,
            proto.output_tokens,
            proto.run_id or None,
            proto.parent_run_id or None,
        )
        if controller._streamed and not controller._ui_finalized:
            controller._ui_finalized = True
            controller._finalize_spinner()
            controller._status.end_run()
            controller._status.update_context_usage()
        elif controller._spinner:
            # Mid-run iteration finished. The next model call hasn't
            # started yet — flip the label back to "Thinking" so the
            # idle counter in the activity widget resets and the user
            # sees a fresh signal rather than a stale "Streaming".
            controller._spinner.set_label("Thinking")

    elif isinstance(proto, msg.RunStarted):
        await on_agent_started(
            controller,
            proto.agent_name,
            proto.run_id,
            proto.parent_run_id or None,
            proto.model,
        )

    elif isinstance(proto, msg.RunCompleted):
        if proto.run_id:
            on_agent_completed(controller, proto.run_id, proto.parent_run_id or None)

    elif isinstance(proto, msg.StreamingDone):
        # Agent's content stream is over — user POV says "done"
        # even though Agno's post-stream tail (compression,
        # memory, final persistence) is still running. Mark the
        # controller as not-processing so ``process_message``
        # accepts the next user input immediately; the BE
        # serialises the actual ``team.arun`` calls behind its
        # own lock, so a follow-up submit just waits silently
        # there until the tail finishes — the user sees the
        # normal "Thinking" UI rather than a stale queue panel.
        controller._processing = False
        controller._sync_queue_panel()

    elif isinstance(proto, msg.RunError):
        await on_run_error(controller, proto.error)

    elif isinstance(proto, msg.ReasoningStarted):
        if controller._spinner:
            controller._spinner.set_label("Reasoning")

    elif isinstance(proto, msg.TaskCreated):
        await controller._ensure_task_progress()
        controller._task_progress.on_task_created(
            task_id=proto.task_id,
            title=proto.title,
            assignee=proto.assignee or None,
            status=proto.status,
        )
        controller._auto_scroll()

    elif isinstance(proto, msg.TaskUpdated):
        await controller._ensure_task_progress()
        controller._task_progress.on_task_updated(
            task_id=proto.task_id,
            status=proto.status,
            assignee=proto.assignee or None,
        )
        controller._auto_scroll()

    elif isinstance(proto, msg.TaskIteration):
        await controller._ensure_task_progress()
        controller._task_progress.on_iteration(proto.iteration, proto.max_iterations)
        if controller._spinner:
            controller._spinner.set_label(f"Iteration {proto.iteration}")
        controller._auto_scroll()

    elif isinstance(proto, msg.TaskStateUpdated):
        await controller._ensure_task_progress()
        if proto.tasks:
            controller._task_progress.on_task_state_updated(proto.tasks)
            controller._auto_scroll()

    else:
        logger.debug("Unhandled protocol message: %s", type(proto).__name__)


# ── Content ───────────────────────────────────────────────────


async def on_content_chunk(controller: "RunController", chunk: str) -> None:
    """Route streamed content to thinking (dimmed) or response widget.

    Models wrap thinking in ``<think>...</think>`` tags within
    the content stream. We detect the tags and split
    accordingly.
    """
    # Check for <think> open tag.
    if not controller._in_thinking and "<think>" in chunk:
        controller._in_thinking = True
        controller._model_uses_think_tags = True
        chunk = chunk.split("<think>", 1)[1]
        if not chunk:
            return

    # Check for </think> close tag — handles both:
    # 1. Normal: <think>..content..</think> (in_thinking=True)
    # 2. Post-tool: content..</think> (model resumes thinking
    #    without open tag)
    if "</think>" in chunk:
        before, after = chunk.split("</think>", 1)
        if before:
            await append_thinking(controller, before)
        controller._in_thinking = False
        if controller._thinking_widget is not None:
            controller._thinking_widget.finalize()
            controller._thinking_widget = None
        after = after.lstrip("\n")
        if after:
            await append_content(controller, after)
        return

    if controller._in_thinking:
        await append_thinking(controller, chunk)
    else:
        await append_content(controller, chunk)


async def append_thinking(controller: "RunController", text: str) -> None:
    """Stream thinking text in dimmed style."""
    if controller._thinking_widget is None:
        if controller._spinner:
            controller._spinner.set_label("Thinking")
        controller._thinking_widget = StreamingMessageWidget(css_class="thinking")
        await controller._mount_target.mount(controller._thinking_widget)
    controller._thinking_widget.append_chunk(text)
    controller._auto_scroll()


async def append_content(controller: "RunController", text: str) -> None:
    """Stream response content in normal style."""
    if controller._stream_widget is None:
        if controller._spinner:
            controller._spinner.set_label("Streaming")
        controller._stream_widget = StreamingMessageWidget()
        await controller._mount_target.mount(controller._stream_widget)
    controller._stream_widget.append_chunk(text)
    controller._auto_scroll()


# ── Tool calls ────────────────────────────────────────────────


async def on_tool_started(
    controller: "RunController",
    friendly: str,
    raw_name: str,
    args_summary: str,
    run_id: str | None,
) -> None:
    """Finalize streaming/thinking widgets, mount the live tool card."""
    if controller._stream_widget is not None:
        controller._stream_widget.finalize()
        controller._stream_widget = None
    if controller._thinking_widget is not None:
        controller._thinking_widget.finalize()
        controller._thinking_widget = None
    controller._in_thinking = False

    if controller._spinner:
        controller._spinner.set_label(f"Running {friendly}")
        if run_id and isinstance(controller._spinner, AgentActivityWidget):
            controller._spinner.on_agent_tool_started(run_id, friendly)

    preview_lines = controller._app.settings.display.tool_result_preview_lines
    widget = ToolCallLiveWidget(
        friendly,
        args_summary,
        status="running",
        preview_lines=preview_lines,
    )
    await controller._mount_target.mount(widget)
    controller._auto_scroll()

    # Wire live progress for orchestrate tools
    # (spawn_agent/spawn_team).
    if raw_name in ("spawn_agent", "spawn_team"):
        wire_orchestrate_progress(controller, widget)


def wire_orchestrate_progress(
    controller: "RunController", widget: ToolCallLiveWidget
) -> None:
    """Set up live progress updates for orchestrate tool calls."""

    def _progress(line: str, w: ToolCallLiveWidget = widget) -> None:
        # Schedule on Textual's message queue to ensure render.
        if controller._app:
            controller._app.call_later(w.update_progress, line)
            controller._app.call_later(controller._auto_scroll)
        else:
            w.update_progress(line)

    controller._app.backend.wire_orchestrate_progress(_progress)


def on_tool_completed(
    controller: "RunController",
    summary: str,
    full_result: str,
    run_id: str | None,
    has_markup: bool = False,
    diff_rows: Any = None,
    is_error: bool = False,
) -> None:
    """Flip the topmost running tool widget to done/error state."""
    # Rebuild Rich diff tables from serializable rows if
    # provided.
    diff_table = None
    if diff_rows and isinstance(diff_rows, (list, tuple)):
        collapsed_table = _build_diff_table(diff_rows, max_rows=4)
        expanded_table = _build_diff_table(diff_rows)
        diff_table = (collapsed_table, expanded_table)

    try:
        for w in reversed(list(controller._mount_target.query(ToolCallLiveWidget))):
            if w.is_running():
                # Flip the widget into the error display *before*
                # ``mark_done`` rerenders so the header swaps
                # from the running ⏳ glyph straight to ✗ instead
                # of flashing ✓ first.
                if is_error:
                    w.mark_error(summary)
                w.mark_done(summary, full_result, has_markup=has_markup, diff_table=diff_table)
                break
    except Exception as exc:
        logger.debug("Failed to mark tool completed in widget: %s", exc)

    if controller._spinner:
        controller._spinner.set_label("Thinking")
        if run_id and isinstance(controller._spinner, AgentActivityWidget):
            controller._spinner.on_agent_tool_completed(run_id)

    # After a tool call, models that use <think> tags typically
    # resume thinking without an opening tag (only emitting
    # </think> to close). Pre-enter thinking mode only if
    # we've seen <think> tags before.
    if controller._model_uses_think_tags:
        controller._in_thinking = True


def on_tool_error(controller: "RunController", error: str) -> None:
    """Agno raised a tool-side exception. Same pattern as
    :func:`on_tool_completed` with ``is_error=True`` —
    ``mark_error`` before ``mark_done`` so the widget flips to
    ✗ instead of rendering ✓ with red error text underneath
    (the v0.5.11 green-check-on-failure class of bug)."""
    summary = f"Error: {error[:60]}"
    try:
        for w in reversed(list(controller._mount_target.query(ToolCallLiveWidget))):
            if w.is_running():
                w.mark_error(summary)
                w.mark_done(summary)
                break
    except Exception as exc:
        logger.debug("Failed to mark tool error in widget: %s", exc)
    if controller._spinner:
        controller._spinner.set_label("Thinking")


# ── Tokens ────────────────────────────────────────────────────


def on_tokens(
    controller: "RunController",
    input_t: int,
    output_t: int,
    run_id: str | None,
    parent_run_id: str | None,
) -> None:
    """Forward per-model-call tokens to the in-flight spinner only.

    The status bar's context-fill indicator and the
    auto-compaction trigger both read from the backend's
    locally-counted total — not from API-reported
    ``input_tokens``, which inflates with
    ``cache_read_input_tokens`` on prompt-caching providers.
    """
    del parent_run_id  # accepted for compatibility, not used
    if controller._spinner and isinstance(controller._spinner, AgentActivityWidget):
        if run_id:
            controller._spinner.on_agent_tokens(run_id, input_t, output_t)
        controller._spinner.set_tokens(input_t + output_t)


# ── Agent lifecycle ───────────────────────────────────────────


async def on_agent_started(
    controller: "RunController",
    name: str,
    run_id: str,
    parent_run_id: str | None,
    model: str,
) -> None:
    """Mount an agent-run container (indented for sub-agents),
    push it on ``controller._agent_stack``. Skip duplicate
    run_ids (e.g. from acontinue_run after HITL)."""
    if controller._spinner and isinstance(controller._spinner, AgentActivityWidget):
        controller._spinner.on_agent_started(name, run_id, parent_run_id, model)

    if run_id in controller._seen_run_ids:
        return
    controller._seen_run_ids.add(run_id)

    # Create agent container — sub-agents get indented.
    is_sub = parent_run_id is not None and len(controller._agent_stack) > 0
    container = AgentRunContainer(
        agent_name=name,
        run_id=run_id,
        model=model,
        is_sub_agent=is_sub,
    )
    # Mount into parent agent's body or the conversation root.
    target = controller._mount_target
    await target.mount(container)
    controller._agent_stack.append((container, run_id))
    controller._auto_scroll()


def on_agent_completed(
    controller: "RunController",
    run_id: str,
    parent_run_id: str | None,
) -> None:
    """Pop the agent from ``controller._agent_stack``. Finalize
    any lingering stream widget."""
    del parent_run_id  # accepted for compatibility, not used
    if controller._spinner and isinstance(controller._spinner, AgentActivityWidget):
        controller._spinner.on_agent_completed(run_id)

    if controller._agent_stack and controller._agent_stack[-1][1] == run_id:
        controller._agent_stack.pop()

    if controller._stream_widget is not None:
        controller._stream_widget.finalize()
        controller._stream_widget = None


# ── Run error ─────────────────────────────────────────────────


async def on_run_error(controller: "RunController", error: str) -> None:
    """Mount a red error line at the current run's target."""
    await controller._mount_target.mount(
        Static(f"[red]Error: {error[:120]}[/red]", classes="run-error")
    )
    controller._auto_scroll()
