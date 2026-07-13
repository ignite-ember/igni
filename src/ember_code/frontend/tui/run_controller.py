"""RunController — thin FE layer that renders protocol messages from the backend.

Streams protocol messages from BackendServer.run_message(), renders them
to Textual widgets, and manages FE-only state (spinners, token counts,
message queue). Zero Agno imports.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from textual.widgets import Static

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.frontend.tui.widgets import (
    AgentActivityWidget,
    AgentRunContainer,
    QueuePanel,
    SpinnerWidget,
    StreamingMessageWidget,
    TaskProgressWidget,
    ToolCallLiveWidget,
)
from ember_code.frontend.tui.widgets._constants import AUTO_SCROLL_THRESHOLD
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_events import _build_diff_table

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp
    from ember_code.frontend.tui.conversation_view import ConversationView
    from ember_code.frontend.tui.hitl_handler import HITLHandler
    from ember_code.frontend.tui.status_tracker import StatusTracker

logger = logging.getLogger(__name__)


class RunController:
    """Thin controller — calls team.arun() directly, dispatches Agno events to TUI.

    Responsibilities:
    - Stream Agno events and update TUI widgets
    - Manage the message queue between runs
    - Delegate HITL confirmations to HITLHandler
    - Track token metrics for the status bar
    """

    def __init__(
        self,
        app: "EmberApp",
        conversation: "ConversationView",
        status: "StatusTracker",
        hitl: "HITLHandler",
    ):
        self._app = app
        self._conversation = conversation
        self._status = status
        self._hitl = hitl

        self._stream_widget: StreamingMessageWidget | None = None
        self._thinking_widget: StreamingMessageWidget | None = None
        self._spinner: AgentActivityWidget | None = None
        self._task_progress: TaskProgressWidget | None = None
        self._processing = False
        # Monotonic counter — bumped at the start of every ``_run``
        # invocation. Lets the finally distinguish "I'm the latest"
        # from "a newer turn took ownership while I was draining the
        # BE tail" and avoid clobbering shared state in the second
        # case.
        self._run_generation = 0
        # Agent nesting — stack of (AgentRunContainer, run_id) pairs
        self._agent_stack: list[tuple[AgentRunContainer, str]] = []
        self._seen_run_ids: set[str] = set()
        self._current_task: asyncio.Task | None = None
        self._queue: list[str] = []
        self._queue_hook: Any = None
        self._streamed = False

        # Turn counter — kept for status-bar / logging only.
        # Memory is now agent-driven (Agno's ``update_user_memory``
        # tool, exposed in AGENTIC mode). The previous every-10-turn
        # blind extraction is gone.
        self._turn_count = 0

        # Hook system messages queued for injection into next AI turn
        self._pending_hook_context: list[str] = []

    # ── Public API ────────────────────────────────────────────────

    @property
    def processing(self) -> bool:
        return self._processing

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    def enqueue(self, message: str) -> int:
        self._queue.append(message)
        self._sync_queue_panel()
        # Forward to BE so its queue hook sees it
        backend = getattr(self._app, "backend", None) if self._app else None
        if backend and hasattr(backend, "_transport"):
            asyncio.ensure_future(backend._transport.send(msg.QueueMessage(text=message)))
        return len(self._queue)

    def dequeue_at(self, index: int) -> str | None:
        if 0 <= index < len(self._queue):
            msg = self._queue.pop(index)
            self._sync_queue_panel()
            return msg
        return None

    def set_current_task(self, task: asyncio.Task | None) -> None:
        self._current_task = task

    def _has_usable_model(self) -> bool:
        """Check if there's at least one model with valid credentials."""
        settings = self._app.settings
        cloud_token = CloudCredentials(settings.auth.credentials_file).access_token
        for cfg in settings.models.registry.values():
            key = cfg.get("api_key", "")
            if key == "cloud_token" and cloud_token:
                return True
            if key and key != "cloud_token":
                return True
            if cfg.get("api_key_env") or cfg.get("api_key_cmd"):
                return True
        return False

    async def process_message(self, message: str) -> None:
        """Entry point — queue or execute a message."""
        # User input pre-empts any active /loop. The /loop subcommands
        # themselves go through, so the user can /loop stop or check
        # /loop status without killing the loop they just configured.
        # Loop iterations bypass this method (they call _run directly
        # via _check_loop_continuation), so anything landing here is
        # by definition fresh user input.
        if not message.startswith("/loop"):
            cancelled = await self._app.backend.cancel_pending_loop()
            if cancelled:
                self._conversation.append_info("Loop interrupted by user input.")
        # Slash commands always run immediately (they don't use the agent)
        if message.startswith("/"):
            await self._run(message)
            return
        if self._processing:
            pos = self.enqueue(message)
            self._conversation.append_info(
                f"Queued (position {pos}). Agent will see it between steps."
            )
            return
        await self._run(message)

    def cancel(self) -> None:
        if not self._processing:
            return

        self._app.backend.cancel_run()

        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

        self._processing = False
        self._current_task = None
        self._queue.clear()
        self._cleanup_spinners()
        self._conversation.append_info("Cancelled.")
        self._sync_queue_panel()

    # ── Main run loop ─────────────────────────────────────────────

    async def _run(self, message: str, *, display: str | None = None) -> None:
        # ``display`` lets callers show one string in chat while
        # sending a different one to the agent. The loop machinery
        # uses this so the user sees the bare prompt while the
        # agent gets the ``<loop-iteration>`` wrapper that tells it
        # not to ask questions between iterations. When unset,
        # display IS the message (the normal case).
        self._conversation.append_user(display if display is not None else message)

        # Inject accumulated shell context (from ! commands) into the message
        # after displaying — the user sees clean text, the AI gets the context
        shell_ctx = self._app._shell_context
        if shell_ctx and not message.startswith("/"):
            context = "\n\n".join(shell_ctx)
            message = f"<shell-context>\n{context}\n</shell-context>\n\n{message}"
            shell_ctx.clear()

        # Slash commands — handled by backend, result rendered by FE
        if message.startswith("/"):
            try:
                logger.debug("Dispatching command: %s", message)
                proto = await self._app.backend.handle_command(message)
                logger.debug("Command result: kind=%s action=%s", proto.kind, proto.action)
                from ember_code.protocol.messages import CommandResult

                result = CommandResult(
                    kind=proto.kind,
                    content=proto.content,
                    action=proto.action,
                    display_content=getattr(proto, "display_content", "") or "",
                )
                self._app.render_command_result(result)
            except Exception as e:
                logger.error("Command failed: %s", e, exc_info=True)
                self._conversation.append_error(f"Command failed: {e}")
            return

        # Check if the user has a usable model configured
        if not self._has_usable_model():
            self._conversation.append_error(
                "No model configured. Either:\n"
                "  - Run /login to use Ember Cloud\n"
                "  - Or add a model to ~/.ember/config.yaml — "
                "see https://ignite-ember.sh/docs/configuration"
            )
            return

        # @file mentions and media detection happen on BE side
        # FE sends raw text — BE processes it before passing to agent

        # Inject queued hook context into the message
        if self._pending_hook_context:
            hook_ctx = "\n".join(self._pending_hook_context)
            message = f"{message}\n<hook-context>{hook_ctx}</hook-context>"
            self._pending_hook_context.clear()

        # ── FE: prepare UI ──
        self._spinner = AgentActivityWidget(label="Thinking")
        self._stream_widget = None
        self._thinking_widget = None
        self._in_thinking = False
        self._model_uses_think_tags = False
        await self._conversation.container.mount(self._spinner)
        self._auto_scroll()
        self._status.start_run()
        self._processing = True
        # Generation tag so a newer ``_run`` task (started while this
        # one is still draining its BE tail) can take ownership of
        # ``_processing`` without an outgoing finally overwriting it.
        # Without this, a follow-up submit during the tail flips
        # ``_processing`` False — the new turn is in flight — and
        # the old run's finally races back to clear it again.
        self._run_generation += 1
        my_generation = self._run_generation

        # Reset per-run state
        self._run_output_text: list[str] = []
        self._last_token_update = 0.0
        self._streamed = False
        self._ui_finalized = False
        self._agent_stack.clear()
        self._seen_run_ids.clear()

        backend = self._app.backend

        # ── Stream from backend ──
        _llm_log = logging.getLogger("ember_code.llm_calls")
        _llm_log.info("RUN START | msg_len=%d", len(message))
        _run_t0 = time.monotonic()
        _chunk_count = 0
        _content_count = 0
        _last_chunk_time = _run_t0

        try:
            backend = self._app.backend
            async for proto in backend.run_message(message):
                _chunk_count += 1
                _now = time.monotonic()
                _gap = _now - _last_chunk_time
                _last_chunk_time = _now

                ptype = type(proto).__name__
                if _gap > 5.0 or _chunk_count <= 3 or _chunk_count % 50 == 0:
                    _llm_log.info(
                        "RUN CHUNK #%d | type=%s | gap=%.1fs | elapsed=%.1fs",
                        _chunk_count,
                        ptype,
                        _gap,
                        _now - _run_t0,
                    )

                # Handle errors/info from backend
                if isinstance(proto, msg.Error):
                    self._conversation.append_error(proto.text)
                    continue
                if isinstance(proto, msg.Info):
                    self._conversation.append_info(proto.text)
                    continue
                if isinstance(proto, msg.RunPaused):
                    await self._handle_hitl_pause(proto, backend, _llm_log)
                    continue

                await self._render(proto)

            _elapsed = time.monotonic() - _run_t0
            _llm_log.info(
                "RUN DONE | chunks=%d | elapsed=%.1fs",
                _chunk_count,
                _elapsed,
            )
        except Exception as e:
            _llm_log.error("RUN ERROR | chunks=%d | error=%s", _chunk_count, e)
            self._conversation.append_error(f"Error: {e}")
            logger.exception("Run error: %s", e)

        # ── FE: finalize UI ──
        if not getattr(self, "_ui_finalized", False):
            self._finalize_spinner()
            self._status.end_run()
            # ``update_context_usage`` is also called from
            # ``_post_run_compaction`` once the backend has counted —
            # this initial call clears the indicator pending the
            # asynchronous refresh so the bar doesn't show stale data.
            self._status.update_context_usage()
        self._ui_finalized = False

        # Only clear ``_processing`` if no newer ``_run`` has taken
        # ownership in the meantime. The flag is normally cleared at
        # ``StreamingDone`` time (well before this finally runs), so
        # this is the safety net for runs that error / get cancelled
        # before content streaming finishes. Same guard for
        # ``_current_task`` — the new turn's task pointer must not be
        # clobbered by the old turn's cleanup.
        if my_generation == self._run_generation:
            self._processing = False
            self._current_task = None

        # ── Background post-run work (non-blocking) ──
        self._turn_count += 1
        # Note: we no longer auto-extract learnings every N turns. The
        # agent now drives memory itself via the ``update_user_memory``
        # tool that Agno's LearningMachine exposes when configured in
        # AGENTIC mode (see ``core/learn.py``). Periodic blind
        # extraction was firing model calls on every 10th turn even
        # when nothing memorable had been said. Turn count is still
        # tracked because ``_post_run_compaction`` references it
        # indirectly via the status bar.

        asyncio.create_task(self._post_run_compaction())
        await self._drain_queue()

    async def _post_run_compaction(self) -> None:
        """Refresh the context-fill indicator and run the compaction
        check, both from Agno's locally-counted token total.

        We used to read ``input_tokens`` off the wire and feed it into
        both consumers. On prompt-caching providers (Anthropic) that
        number includes ``cache_read_input_tokens`` cumulated across
        tool iterations — millions of tokens after a few turns — which
        spuriously triggered the 80% auto-compaction → history wipe.
        Asking the backend to ``count_tokens(messages)`` produces the
        actual conversation size instead.
        """
        try:
            backend = self._app.backend
            ctx_tokens = await backend.count_context_tokens()
            max_ctx = self._status.max_context_tokens
            self._status.set_context_tokens(ctx_tokens)
            self._status.update_context_usage()
            result = await backend.compact_if_needed(ctx_tokens, max_ctx)
            if result:
                self._conversation.append_info(
                    "Context auto-compacted — older messages summarized."
                )
                if result.summary:
                    self._conversation.append_info(f"Summary: {result.summary}")
                self._status.set_context_tokens(0)
                self._status.update_context_usage()
        except Exception as e:
            logger.debug("Post-run compaction failed: %s", e)

    async def _drain_queue(self) -> None:
        if self._queue:
            next_msg = self._queue.pop(0)
            self._sync_queue_panel()
            await self._run(next_msg)
            return
        # Queue empty — check whether a /loop wants the next turn.
        await self._check_loop_continuation()

    async def _check_loop_continuation(self) -> None:
        """If a ``/loop`` is active, fire its next iteration.

        The backend owns the iteration counter — we ask it for the next
        prompt and it returns ``None`` when the loop is exhausted or
        was cancelled. Iterations call ``_run`` directly, bypassing the
        ``process_message`` entry point so they don't trigger the
        user-input cancellation guard.
        """
        try:
            descriptor = await self._app.backend.pop_pending_loop_iteration()
        except Exception:
            logger.debug("pop_pending_loop_iteration failed", exc_info=True)
            return
        if not descriptor:
            return
        # Completion marker — one-shot signal that the loop just hit
        # its cap and was cleared on the backend. Render a summary so
        # the user knows the loop ended naturally; don't recurse.
        if descriptor.get("completed"):
            total = descriptor.get("total_iterations", 0)
            self._conversation.append_info(
                f"✓ Loop completed after {total} iteration{'s' if total != 1 else ''}."
            )
            return
        # Safety-cap pause marker — implicit loop hit
        # ``LOOP_HARD_CAP``. The backend has already flipped the
        # loop to paused; the user decides whether to continue
        # (``/loop resume``) or terminate (``/loop stop``). Don't
        # recurse — paused loops short-circuit ``advance_loop``.
        if descriptor.get("safety_cap_paused"):
            n = descriptor.get("iteration", 0)
            self._conversation.append_info(
                f"⏸ Loop paused at iteration {n} — safety ceiling reached. "
                f"Run /loop resume to continue, or /loop stop to terminate."
            )
            return
        prompt = descriptor["prompt"]
        iteration = descriptor.get("iteration", 0)
        remaining = descriptor.get("remaining", 0)
        # The descriptor's ``prompt`` is wrapped with the autonomous-
        # loop ``<loop-iteration>`` meta tag so the agent doesn't ask
        # the user between iterations. ``display_prompt`` is the
        # original (unwrapped) string — what the user sees in chat.
        display = descriptor.get("display_prompt") or prompt
        # When the implicit safety cap just expanded itself for
        # another batch, surface a one-shot info line so the user
        # knows the loop hasn't been silently retrofitted with a
        # new ceiling — they can still ``/loop stop`` if they
        # think it should end.
        if descriptor.get("auto_extended"):
            self._conversation.append_info(
                "↻ Safety cap reached — auto-extending the loop. Run `/loop stop` to terminate."
            )
        # Visible iteration banner so the user has an anchor between
        # iterations. The "N remaining" half is only meaningful when
        # the run is explicitly capped — for an implicit safety
        # net "X remaining" is misleading since the cap auto-extends.
        if descriptor.get("cap_explicit"):
            banner = f"↻ Loop iteration {iteration} ({remaining} remaining after this one)"
        else:
            banner = f"↻ Loop iteration {iteration}"
        self._conversation.append_info(banner)
        # Wrap the iteration's ``_run`` so any unhandled error
        # (429 from the model API past Agno's retries, network
        # failure, tool exception, etc.) *pauses* the loop instead
        # of advancing. The counter stays at the failing iteration
        # N, so a subsequent ``/loop resume`` retries N rather
        # than skipping to N+1.
        try:
            await self._run(prompt, display=display)
        except Exception as e:
            logger.exception("Loop iteration %d failed", iteration)
            try:
                await self._app.backend.loop_pause()
            except Exception:
                # If even pausing fails, fall through — the loop
                # is in a degraded state but we've at least logged
                # the original error.
                logger.debug("loop_pause RPC also failed", exc_info=True)
            self._conversation.append_error(
                f"⏸ Loop paused after iteration {iteration} failed: {e}. "
                f"Run /loop resume to retry, or /loop stop to terminate."
            )

    # ── Render protocol messages ─────────────────────────────────

    async def _render(self, proto: Any) -> None:
        """See :func:`tui.run_renderer.render`."""
        from ember_code.frontend.tui.run_renderer import render

        await render(self, proto)

    # ── Content ───────────────────────────────────────────────────

    async def _on_content_chunk(self, chunk: str) -> None:
        """See :func:`tui.run_renderer.on_content_chunk`."""
        from ember_code.frontend.tui.run_renderer import on_content_chunk

        await on_content_chunk(self, chunk)

    async def _append_thinking(self, text: str) -> None:
        """See :func:`tui.run_renderer.append_thinking`."""
        from ember_code.frontend.tui.run_renderer import append_thinking

        await append_thinking(self, text)

    async def _append_content(self, text: str) -> None:
        """See :func:`tui.run_renderer.append_content`."""
        from ember_code.frontend.tui.run_renderer import append_content

        await append_content(self, text)

    # ── Tool calls ────────────────────────────────────────────────

    async def _on_tool_started(
        self, friendly: str, raw_name: str, args_summary: str, run_id: str | None
    ) -> None:
        """See :func:`tui.run_renderer.on_tool_started`."""
        from ember_code.frontend.tui.run_renderer import on_tool_started

        await on_tool_started(self, friendly, raw_name, args_summary, run_id)

    def _wire_orchestrate_progress(self, widget: ToolCallLiveWidget) -> None:
        """See :func:`tui.run_renderer.wire_orchestrate_progress`."""
        from ember_code.frontend.tui.run_renderer import wire_orchestrate_progress

        wire_orchestrate_progress(self, widget)

    def _on_tool_completed(
        self,
        summary: str,
        full_result: str,
        run_id: str | None,
        has_markup: bool = False,
        diff_rows: Any = None,
        is_error: bool = False,
    ) -> None:
        """See :func:`tui.run_renderer.on_tool_completed`."""
        from ember_code.frontend.tui.run_renderer import on_tool_completed

        on_tool_completed(self, summary, full_result, run_id, has_markup, diff_rows, is_error)

    def _on_tool_error(self, error: str) -> None:
        """See :func:`tui.run_renderer.on_tool_error`."""
        from ember_code.frontend.tui.run_renderer import on_tool_error

        on_tool_error(self, error)

    # ── Tokens ────────────────────────────────────────────────────

    def _on_tokens(
        self, input_t: int, output_t: int, run_id: str | None, parent_run_id: str | None
    ) -> None:
        """See :func:`tui.run_renderer.on_tokens`."""
        from ember_code.frontend.tui.run_renderer import on_tokens

        on_tokens(self, input_t, output_t, run_id, parent_run_id)

    # ── Agent lifecycle ───────────────────────────────────────────

    async def _on_agent_started(
        self, name: str, run_id: str, parent_run_id: str | None, model: str
    ) -> None:
        """See :func:`tui.run_renderer.on_agent_started`."""
        from ember_code.frontend.tui.run_renderer import on_agent_started

        await on_agent_started(self, name, run_id, parent_run_id, model)

    def _on_agent_completed(self, run_id: str, parent_run_id: str | None) -> None:
        """See :func:`tui.run_renderer.on_agent_completed`."""
        from ember_code.frontend.tui.run_renderer import on_agent_completed

        on_agent_completed(self, run_id, parent_run_id)

    # ── Run error ─────────────────────────────────────────────────

    async def _on_run_error(self, error: str) -> None:
        """See :func:`tui.run_renderer.on_run_error`."""
        from ember_code.frontend.tui.run_renderer import on_run_error

        await on_run_error(self, error)

    # ── HITL ──────────────────────────────────────────────────────

    async def _handle_hitl_pause(self, proto, backend, _llm_log) -> None:
        """Handle a RunPaused protocol message — collect every decision
        then resolve them all in one round-trip.

        The previous implementation iterated ``proto.requirements`` and
        called ``backend.resolve_hitl(req_id, ...)`` per req, each of
        which called ``acontinue_run(requirements=[req])`` with only
        the one resolved requirement. Agno treats requirements absent
        from the resolution list as denied, so a parallel 8-tool plan
        had its first call succeed and the rest reported back as
        "User denied" — the LLM rendered the others as REJECTED even
        though the user never saw a reject dialog. Collecting every
        decision and shipping a single ``resolve_hitl_batch`` lets
        Agno see the full set.
        """
        _llm_log.info("HITL PAUSE: %d requirements", len(proto.requirements))
        if self._stream_widget is not None:
            self._stream_widget.finalize()
            self._stream_widget = None
        if self._spinner:
            self._spinner.set_label("Awaiting confirmation")

        decisions: list[tuple[str, str, str]] = []
        for req in proto.requirements:
            _llm_log.info("HITL: showing dialog for %s", req.tool_name)
            action, choice = await self._hitl.handle_protocol(req)
            _llm_log.info("HITL: user chose %s/%s for %s", action, choice, req.requirement_id)
            decisions.append((req.requirement_id, action, choice))

            if action == "reject":
                # Show a denied tool widget so the user sees what happened
                friendly = req.friendly_name or req.tool_name
                args = req.tool_args or {}
                # Build a short args summary like "$ pwd" or "file.py"
                if "args" in args and isinstance(args["args"], list):
                    args_str = " ".join(str(a) for a in args["args"])
                elif "file_path" in args:
                    args_str = str(args["file_path"])
                else:
                    args_str = ", ".join(f"{v}" for v in args.values())[:80]
                widget = ToolCallLiveWidget(
                    friendly,
                    args_str,
                    status="running",
                    preview_lines=self._app.settings.display.tool_result_preview_lines,
                )
                await self._mount_target.mount(widget)
                widget.mark_error("Denied by user")
                widget.mark_done("Denied by user")
                self._auto_scroll()

        if self._spinner:
            self._spinner.set_label("Continuing")

        async for cont_proto in backend.resolve_hitl_batch(decisions):
            # Recursive — continuation may yield another pause
            if isinstance(cont_proto, msg.RunPaused):
                await self._handle_hitl_pause(cont_proto, backend, _llm_log)
            else:
                await self._render(cont_proto)

    # ── Task orchestration ────────────────────────────────────────

    async def _ensure_task_progress(self) -> None:
        """Mount the TaskProgressWidget if not already present."""
        if self._task_progress is None:
            self._task_progress = TaskProgressWidget()
            await self._conversation.container.mount(self._task_progress)

    # ── Debug logging ────────────────────────────────────────────

    def _log_run_messages(self, team: Any) -> None:
        """Dump the messages from the last run for debugging tool result delivery."""
        try:
            rr = getattr(team, "run_response", None)
            if rr is None:
                logger.debug("RUN_MESSAGES: no run_response on team")
                return

            # Get messages from the run response
            messages = getattr(rr, "messages", None)
            if messages:
                logger.debug("RUN_MESSAGES: %d messages in run_response", len(messages))
                for i, msg in enumerate(messages):
                    role = getattr(msg, "role", "?")
                    content = getattr(msg, "content", None)
                    tool_calls = getattr(msg, "tool_calls", None)
                    tool_call_id = getattr(msg, "tool_call_id", None)
                    compressed = getattr(msg, "compressed_content", None)
                    from_hist = getattr(msg, "from_history", False)

                    content_preview = ""
                    if content is not None:
                        content_str = str(content)
                        content_preview = content_str[:200]
                        if len(content_str) > 200:
                            content_preview += f"... ({len(content_str)} total chars)"

                    extras = []
                    if tool_call_id:
                        extras.append(f"tool_call_id={tool_call_id}")
                    if tool_calls:
                        tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                        extras.append(f"tool_calls={tc_names}")
                    if compressed is not None:
                        extras.append(f"COMPRESSED len={len(str(compressed))}")
                    if from_hist:
                        extras.append("from_history")

                    extra_str = " | ".join(extras) if extras else ""
                    logger.debug(
                        "  MSG[%d] role=%s %s content=%.200s",
                        i,
                        role,
                        extra_str,
                        content_preview,
                    )
            else:
                logger.debug("RUN_MESSAGES: no messages in run_response")

            # Also log the run_response content
            resp_content = getattr(rr, "content", None)
            if resp_content:
                logger.debug(
                    "RUN_RESPONSE content (len=%d): %.300s",
                    len(str(resp_content)),
                    str(resp_content)[:300],
                )
        except Exception as e:
            logger.debug("RUN_MESSAGES: error dumping messages: %s", e)

    # ── Helpers ───────────────────────────────────────────────────

    @property
    def _mount_target(self):
        """Current container to mount widgets into — agent body or conversation."""
        if self._agent_stack:
            return self._agent_stack[-1][0].body
        return self._conversation.container

    def _auto_scroll(self) -> None:
        c = self._conversation.container
        if c.max_scroll_y - c.scroll_y < AUTO_SCROLL_THRESHOLD:
            c.scroll_end(animate=False)

    def _sync_queue_panel(self) -> None:
        try:
            panel = self._app.query_one("#queue-panel", QueuePanel)
            panel.refresh_items(list(self._queue))
        except Exception as exc:
            logger.debug("Failed to sync queue panel: %s", exc)

    def _finalize_spinner(self) -> None:
        if self._spinner:
            try:
                self._spinner.stop()
                self._spinner.remove()
            except Exception as exc:
                logger.debug("Failed to finalize spinner: %s", exc)
            self._spinner = None
        # Task progress widget stays visible after run completes (read-only)
        self._task_progress = None

    def _cleanup_spinners(self) -> None:
        for cls in (SpinnerWidget, AgentActivityWidget):
            try:
                for s in self._app.query(cls):
                    s.stop()
                    s.remove()
            except Exception as exc:
                logger.debug("Failed to cleanup spinner %s: %s", cls.__name__, exc)
