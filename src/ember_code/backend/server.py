"""Backend server — processes FE messages and streams BE events.

Owns the Session object and all Agno/AI logic. The FE never touches
Session directly — everything goes through protocol messages.

In Phase 2 (single-process), this is called in-process by the TUI.
In Phase 4 (multi-process), this runs as a separate process with
socket transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.protocol import messages as msg
from ember_code.protocol.serializer import serialize_event

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.models import MarketplaceInfo, PluginInfo
    from ember_code.core.pool import AgentInfo
    from ember_code.core.skills.parser import SkillInfo

logger = logging.getLogger(__name__)


class BackendServer:
    """Wraps Session and handles all FE→BE protocol messages."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ):
        from ember_code.core.session import Session

        self._session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        self._settings = settings
        self._pending_requirements: dict[str, Any] = {}  # requirement_id → Agno requirement
        self._processing = False
        self._current_team: Any = None  # held during HITL pause

    # No .session property — all access goes through backend methods

    # ── Run a user message (streaming) ────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, Any] | None = None
    ) -> AsyncIterator[msg.Message]:
        """Execute a user message and yield protocol messages.

        This is the main streaming entry point. The FE iterates over
        the yielded messages and renders them.
        """
        from ember_code.core.hooks.events import HookEvent

        self._processing = True
        team = self._session.main_team

        # Process @file mentions
        from ember_code.core.utils.mentions import process_file_mentions

        text, mentioned_files = process_file_mentions(text)
        if mentioned_files:
            yield msg.Info(text=f"Referenced: {', '.join(mentioned_files)}")

        # Resolve bare filenames and attach media for vision-capable models
        from ember_code.core.utils.media import resolve_file_references

        model_name = self._session.settings.models.default
        model_cfg = self._session.settings.models.registry.get(model_name, {})
        is_vision = model_cfg.get("vision", False)

        text, resolved_files = resolve_file_references(text, project_dir=self._session.project_dir)
        if resolved_files:
            if is_vision:
                from ember_code.core.utils.media import attach_resolved_files

                parsed_media = attach_resolved_files(resolved_files)
                if parsed_media:
                    media = parsed_media
                    yield msg.Info(text=f"Attached: {len(resolved_files)} file(s)")
                else:
                    yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")
            else:
                yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")

        # Attach media URLs (images, etc.) for vision models
        if is_vision:
            from ember_code.core.utils.media import extract_media_urls

            url_media = extract_media_urls(text)
            if url_media:
                if media:
                    for k, v in url_media.items():
                        media.setdefault(k, []).extend(v)
                else:
                    media = url_media
                count = sum(len(v) for v in url_media.values())
                yield msg.Info(text=f"Attached {count} URL(s)")

        # Inject learnings
        await self._session._inject_learnings()

        # Add timestamp
        from datetime import datetime

        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        message = f"<system-context>Current datetime: {timestamp}</system-context>\n{text}"

        # Fire UserPromptSubmit hook
        hook_result = await self._session.hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload={"message": text, "session_id": self._session.session_id},
        )
        if not hook_result.should_continue:
            yield msg.Error(text=hook_result.message or "Message blocked by hook.")
            self._processing = False
            return
        if hook_result.message:
            # Queue hook context for injection
            message = f"{message}\n<hook-context>{hook_result.message}</hook-context>"

        # Stream events from Agno. We multiplex the team's stream with
        # the sub-agent HITL coordinator — see ``_stream_with_subagent_hitl``
        # for the full rationale. The same multiplexer is also used by
        # ``resolve_hitl`` so a parent that pauses (top-level Bash) and
        # then spawns a sub-agent on resume still gets the sub-agent's
        # pauses surfaced — an earlier version only multiplexed inside
        # ``run_message`` and the sub-agent's pauses silently sat in the
        # coordinator forever.
        media_kwargs = media or {}
        try:
            async for proto in self._stream_with_subagent_hitl(
                team.arun(message, stream=True, **media_kwargs)
            ):
                yield proto
        finally:
            self._processing = False
            await self._close_model_http_client(team)

        # Fire Stop hook
        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload={"session_id": self._session.session_id},
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    async def _stream_with_subagent_hitl(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """Multiplex a team's event stream with the sub-agent coordinator.

        The team's stream and the sub-agent HITL coordinator are two
        independent producers of messages we need to forward to the FE:

        * ``team_stream`` is whatever Agno is currently driving — the
          initial ``team.arun`` call from ``run_message``, or a
          ``team.acontinue_run`` resumption from ``resolve_hitl``.
        * The coordinator wakes whenever a sub-agent (running inside a
          ``spawn_agent`` tool) hits a ``RunPausedEvent``. We have to
          surface that pause to the FE as a ``RunPaused`` message so the
          dialog appears.

        Both paths must run concurrently with the team stream; otherwise
        a sub-agent that pauses while the parent is still streaming
        events would have its requirement sitting in the coordinator
        forever with no one to forward it. Centralising this here means
        ``run_message`` AND ``resolve_hitl`` both get the multiplexer —
        a previous version had it only in ``run_message`` so any sub-
        agent spawn that happened during a resumed run (parent paused
        for top-level Bash, user approved, parent resumed and then
        spawned an architect) silently dropped the architect's pauses.

        The team's own ``RunPausedEvent`` (parent pauses for its own
        tool) terminates this stream and is forwarded as ``RunPaused``;
        the FE then routes resolution back through ``resolve_hitl``,
        which calls this helper again with the resumed stream.
        """
        from ember_code.protocol.agno_events import RUN_PAUSED_EVENTS

        sub_hitl = self._session.sub_agent_hitl
        agno_queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()
        import logging as _log

        llm_log = _log.getLogger("ember_code.llm_calls")

        # Direct-write trace bypassing the logging stack — see earlier
        # debugging note. Cheap; one flushed write per pump iteration.
        import os as _os
        from pathlib import Path as _Path

        _trace_path = _Path(_os.path.expanduser("~/.ember/hitl_trace.log"))
        _trace_path.parent.mkdir(parents=True, exist_ok=True)

        def _trace(msg_text: str) -> None:
            try:
                with open(_trace_path, "a") as _f:
                    import time as _t

                    _f.write(f"{_t.strftime('%H:%M:%S')} pid={_os.getpid()} {msg_text}\n")
            except Exception:
                pass

        async def _drain_team() -> None:
            try:
                async for event in team_stream:
                    await agno_queue.put(("event", event))
            except Exception as e:
                await agno_queue.put(("error", e))
            finally:
                await agno_queue.put(("done", SENTINEL))

        async def _drain_subagent_hitl() -> None:
            _trace(f"_stream_mux: drain STARTED (coord_id={id(sub_hitl)})")
            try:
                while True:
                    await sub_hitl.new_arrival.wait()
                    entries = sub_hitl.list_new_pending()
                    _trace(f"_stream_mux: drain woke, {len(entries)} entries")
                    if entries:
                        await agno_queue.put(("subagent_pause", entries))
                        _trace(f"_stream_mux: drain enqueued {[rid for rid, _ in entries]}")
            except asyncio.CancelledError:
                _trace("_stream_mux: drain cancelled")
                return

        _trace(f"_stream_mux: starting (coord_id={id(sub_hitl)})")
        # Hold the team-drain reference so the task isn't GC'd mid-run
        # (asyncio only weakly references background tasks). We
        # deliberately don't cancel it in ``finally`` — see comment
        # below.
        _team_task = asyncio.create_task(_drain_team())  # noqa: F841
        sub_task = asyncio.create_task(_drain_subagent_hitl())

        try:
            while True:
                kind, payload = await agno_queue.get()
                if kind == "done":
                    return
                if kind == "error":
                    raise payload
                if kind == "subagent_pause":
                    entries = payload
                    rp = self._build_subagent_run_paused(entries)
                    llm_log.info(
                        "subagent_hitl: yielding RunPaused to FE with %d req(s)",
                        len(entries),
                    )
                    yield rp
                    continue
                event = payload
                if isinstance(event, RUN_PAUSED_EVENTS):
                    for pause_msg in self._handle_pause(event):
                        yield pause_msg
                    return
                proto = serialize_event(event)
                if proto is not None:
                    yield proto
        except asyncio.TimeoutError:
            yield msg.Error(text="Request timed out — the model took too long to respond.")
        except Exception as e:
            yield msg.Error(text=str(e))
        finally:
            sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sub_task
            # Don't cancel team_task — the team's stream may still have
            # a paused tool we want to drive to completion via
            # ``resolve_hitl``. The team task naturally exits when the
            # team's stream ends or errors.

    def _build_subagent_run_paused(self, entries: list) -> msg.Message:
        """Wrap a batch of sub-agent coordinator entries in a ``RunPaused``.

        The FE renders the confirmation dialog only when it sees
        ``RunPaused``; bare ``HITLRequest`` falls through. Sub-agent pauses
        match the FE's expected shape so the same dialog flow applies.
        Resolution still routes through the coordinator (not the main
        team's ``acontinue_run``) — see ``resolve_hitl``.
        """
        from ember_code.protocol.agno_events import TOOL_NAMES

        requirements = []
        for req_id, entry in entries:
            req = entry.requirement
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                    tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
                    agent_path=list(getattr(entry, "agent_path", []) or []),
                )
            )
        # ``run_id`` here is the sub-agent's id; the FE doesn't currently
        # use it for sub-agent pauses (it routes through ``resolve_hitl``
        # which picks the coordinator path), but we forward it for logs.
        sub_run_id = entries[0][1].run_id if entries else ""
        return msg.RunPaused(run_id=sub_run_id, requirements=requirements)

    def _handle_pause(self, event: Any) -> list[msg.Message]:
        """Convert a RunPausedEvent into protocol messages and store requirements."""
        from ember_code.protocol.agno_events import TOOL_NAMES

        run_id = getattr(event, "run_id", None)
        messages = []
        requirements = []
        for req in getattr(event, "active_requirements", []) or []:
            req_id = str(uuid.uuid4())[:8]
            # Store both the requirement and the run_id from the event
            self._pending_requirements[req_id] = (req, run_id)
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                    tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
                )
            )
        messages.append(
            msg.RunPaused(
                run_id=str(getattr(event, "run_id", "") or ""),
                requirements=requirements,
            )
        )
        return messages

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]:
        """Resolve a HITL requirement and continue the run."""
        # Sub-agent pauses go through the coordinator: it wakes up the
        # spawn_agent stream which then issues acontinue_run on the
        # specialist. The parent run is still streaming separately, so
        # we don't yield a RunPaused/Continue envelope here — the FE
        # just knows the prompt is dismissed.
        if self._session.sub_agent_hitl.resolve(requirement_id, action):
            return
        entry = self._pending_requirements.pop(requirement_id, None)
        if entry is None:
            yield msg.Error(text=f"Unknown requirement: {requirement_id}")
            return

        req, run_id = entry  # (requirement, run_id) tuple

        if action == "confirm":
            req.confirm()
        else:
            req.reject(note="User denied")

        # Continue the run via the same multiplexer used by ``run_message``
        # so that any sub-agent pauses fired while the parent is resuming
        # also reach the FE. (Without the multiplexer here, a parent that
        # pauses for top-level Bash, gets resumed, and then spawns an
        # architect would have the architect's pauses dropped on the
        # floor.)
        team = self._session.main_team
        import logging as _log

        _llm = _log.getLogger("ember_code.llm_calls")
        _llm.info("resolve_hitl: action=%s, req_id=%s, run_id=%s", action, requirement_id, run_id)
        async for proto in self._stream_with_subagent_hitl(
            team.acontinue_run(
                run_id=run_id,
                session_id=self._session.session_id,
                requirements=[req],
                stream=True,
                stream_events=True,
            )
        ):
            yield proto

        # Fire Stop hook after continuation completes
        from ember_code.core.hooks.events import HookEvent

        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload={"session_id": self._session.session_id},
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    # ── Commands ──────────────────────────────────────────────────

    async def handle_command(self, text: str) -> msg.CommandResult:
        """Process a slash command and return the result."""
        from ember_code.backend.command_handler import CommandHandler

        handler = CommandHandler(self._session)
        result = await handler.handle(text)
        return msg.CommandResult(
            kind=result.kind,
            content=result.content,
            action=result.action or "",
        )

    # ── Session management ────────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        """List available sessions."""
        raw = await self._session.persistence.list_sessions(limit=20)
        return msg.SessionListResult(sessions=raw)

    async def switch_session(self, session_id: str) -> msg.Info:
        """Switch to a different session."""
        self._session.session_id = session_id
        self._session.session_named = True
        self._session.main_team.session_id = session_id
        self._session.persistence.session_id = session_id

        # Load history — aget_session triggers Agno to restore conversation
        agent = self._session.main_team
        await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        name = await self._session.persistence.get_name()
        return msg.Info(text=f"Switched to session: {name or session_id}")

    # ── MCP ───────────────────────────────────────────────────────

    async def ensure_mcp(self) -> None:
        """Initialize MCP connections."""
        await self._session.ensure_mcp()

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        """Connect or disconnect an MCP server."""
        mgr = self._session.mcp_manager
        if connect:
            await mgr.connect(server_name)
        else:
            await mgr.disconnect_one(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"MCP {'connected' if connect else 'disconnected'}: {server_name}")

    def get_mcp_status(self) -> list[tuple[str, bool]]:
        """Get MCP server connection status."""
        return self._session.get_mcp_status()

    # ── Permissions ────────────────────────────────────────────────

    def check_permission(self, tool_name: str, func_name: str, tool_args: dict) -> str:
        """Check permission level for a tool call. Returns 'allow'/'deny'/'ask'."""
        from ember_code.core.config.tool_permissions import FUNC_TO_TOOL, ToolPermissions

        perms = ToolPermissions(project_dir=self._session.project_dir)
        registry_name = FUNC_TO_TOOL.get(func_name, tool_name)
        return perms.check(registry_name, func_name, tool_args)

    def save_permission_rule(self, rule: str, level: str) -> None:
        """Persist a permission rule."""
        from ember_code.core.config.tool_permissions import ToolPermissions

        perms = ToolPermissions(project_dir=self._session.project_dir)
        perms.save_rule(rule, level)

    # ── Model ─────────────────────────────────────────────────────

    def switch_model(self, model_name: str) -> msg.Info:
        """Switch the active model."""
        old_name = self._session.settings.models.default
        old_cfg = self._session.settings.models.registry.get(old_name, {})
        new_cfg = self._session.settings.models.registry.get(model_name, {})

        self._session.settings.models.default = model_name
        self._session.main_team = self._session._build_main_agent()

        note = f"Switched to {model_name}"
        # Warn if switching from vision to non-vision with media in history
        if old_cfg.get("vision") and not new_cfg.get("vision"):
            note += (
                "\nNote: previous messages may contain images/files. "
                "Use /clear to reset if you get errors."
            )
        return msg.Info(text=note)

    # ── Login/Logout ──────────────────────────────────────────────

    async def login(self, on_status=None) -> tuple[bool, str]:
        """Run the browser-callback login flow.

        Args:
            on_status: optional callback(str) for status updates to FE

        Returns:
            (success, email) tuple
        """
        import webbrowser

        from ember_code.core.auth.client import (
            get_login_url,
            start_callback_server,
            validate_token,
            wait_for_token,
        )
        from ember_code.core.auth.credentials import decode_jwt_claims, save_credentials

        def _status(text: str) -> None:
            if on_status:
                result = on_status(text)
                # Support both sync and async callbacks
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)

        server = None
        try:
            _status("Starting local server...")
            server, callback_url = start_callback_server()
            port = int(callback_url.split(":")[2].split("/")[0])
            login_url = get_login_url(port)

            with contextlib.suppress(Exception):
                webbrowser.open(login_url)

            _status(
                f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{login_url}"
            )

            try:
                token = await wait_for_token(server, timeout=300)
            except TimeoutError:
                return False, "Login timed out"

            _status("Fetching user info...")
            user_info = await validate_token(token, self._settings.api_url)
            email = user_info.get("email", "") if user_info else ""

            # Read expiry from JWT for accurate TTL
            claims = decode_jwt_claims(token)
            if claims.get("exp"):
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
                ttl = max(int((exp - now).total_seconds()), 0)
                save_credentials(token, email, ttl=ttl)
            else:
                save_credentials(token, email)

            self.reload_cloud_credentials()
            return True, email

        except Exception as exc:
            return False, str(exc)
        finally:
            # Always close the callback server to free the port
            if server is not None:
                with contextlib.suppress(Exception):
                    server.server_close()

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """Reload cloud credentials after login."""
        from ember_code.core.auth.credentials import CloudCredentials

        self._session._cloud = CloudCredentials(self._settings.auth.credentials_file)
        self._session.main_team = self._session._build_main_agent()
        return self.get_status()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """Clear cloud credentials on logout."""
        from ember_code.core.auth.credentials import CloudCredentials

        # Point at a path that doesn't exist so all properties resolve to None.
        self._session._cloud = CloudCredentials(path="/dev/null")
        self._session.main_team = self._session._build_main_agent()
        return self.get_status()

    # ── Status ────────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        """Get current status bar data."""
        return msg.StatusUpdate(
            model=self._settings.models.default,
            cloud_connected=self._session.cloud_connected,
            cloud_org=self._session.cloud_org_name or "",
        )

    # ── /loop continuation ────────────────────────────────────────

    async def pop_pending_loop_iteration(self) -> dict | None:
        """Pop the next ``/loop`` iteration descriptor (or completion).

        Three return shapes the FE distinguishes:

        - ``None`` → no loop is or was active. The FE renders nothing.
        - ``{"prompt", "iteration", "remaining"}`` → fire ``prompt`` as
          the next turn. ``iteration`` is the 1-based number of THIS
          iteration; ``remaining`` is how many MORE will follow if the
          cap isn't shortened.
        - ``{"completed": True, "total_iterations": N}`` → the loop just
          hit its cap and was cleared. The FE renders a one-shot
          "loop completed" summary so the user knows the loop ended
          naturally rather than being slow. State is cleared as part
          of this call, so subsequent calls return ``None``.

        Called by the FE's run controller after every ``_drain_queue``
        returns to idle. Keeping the truth on the backend means there's
        one source of state and any cancellation (``/loop stop``, user
        interrupt) is naturally consistent across iterations.
        """
        sess = self._session
        if sess.pending_loop_prompt is None:
            return None
        if sess.loop_iterations_remaining <= 0:
            # Cap exhausted — emit a one-shot completion marker so the
            # FE can render "Loop completed after N iterations", then
            # clear so the next call returns None (no double-render).
            total = sess.loop_iteration_index
            sess.pending_loop_prompt = None
            sess.loop_iteration_index = 0
            return {"completed": True, "total_iterations": total}
        sess.loop_iterations_remaining -= 1
        sess.loop_iteration_index += 1
        return {
            "prompt": sess.pending_loop_prompt,
            "iteration": sess.loop_iteration_index,
            "remaining": sess.loop_iterations_remaining,
        }

    async def cancel_pending_loop(self) -> bool:
        """Clear ``/loop`` state. Returns whether anything was active.

        Called by the FE when the user types a non-``/loop`` message —
        user input always takes precedence over the loop.
        """
        sess = self._session
        if sess.pending_loop_prompt is None:
            return False
        sess.pending_loop_prompt = None
        sess.loop_iterations_remaining = 0
        return True

    # ── Compaction ────────────────────────────────────────────────

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """Compact session if approaching context limit."""
        compacted = await self._session.compact_if_needed(ctx_tokens, max_ctx)
        if compacted:
            summary = ""
            with contextlib.suppress(Exception):
                agno_session = await self._session.main_team.aget_session(
                    session_id=self._session.session_id,
                    user_id=self._session.user_id,
                )
                if agno_session and agno_session.summary and agno_session.summary.summary:
                    summary = agno_session.summary.summary
            return msg.SessionCleared(
                new_session_id=self._session.session_id,
                summary=summary,
            )
        return None

    # ── Learning ──────────────────────────────────────────────────

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Run learning extraction as a background task on the main event loop.

        Uses the main loop (not a separate thread) so the httpx client's
        connection pool works correctly.
        """
        learning = self._session._learning
        if learning is None:
            return

        from agno.models.message import Message as AgnoMessage

        messages = [AgnoMessage(role="user", content=user_msg)]
        if assistant_msg:
            messages.append(AgnoMessage(role="assistant", content=assistant_msg))

        async def _run() -> None:
            try:
                await learning.aprocess(
                    messages=messages,
                    user_id=self._session.user_id,
                    session_id=self._session.session_id,
                )
            except Exception as exc:
                logger.warning("Learning extraction failed: %s", exc)

        asyncio.create_task(_run())

    # ── Cleanup ───────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown — disconnect MCP, fire hooks, kill bg processes."""
        from ember_code.core.hooks.events import HookEvent
        from ember_code.core.tools.shell import EmberShellTools

        with contextlib.suppress(Exception):
            await self._session.hook_executor.execute(
                event=HookEvent.SESSION_END.value,
                payload={"session_id": self._session.session_id},
            )
        with contextlib.suppress(Exception):
            if self._session.settings.orchestration.auto_cleanup:
                self._session.pool.cleanup_ephemeral()
        with contextlib.suppress(Exception):
            await self._session.mcp_manager.disconnect_all()
        with contextlib.suppress(Exception):
            killed = EmberShellTools.cleanup()
            if killed:
                logger.info("Shutdown: killed %d background process(es)", killed)

    # ── Accessors for FE (read-only state) ──────────────────────

    def wire_queue_hook(self, queue: list) -> None:
        """Wire queue hooks onto the team.

        - Tool-hook (injector) drains the queue after each tool call so the
          model sees queued text on its next iteration.
        - Post-hook (persister) records those drained items as proper
          user-role history entries before the session is saved.
        """
        from ember_code.core.queue_hook import create_queue_hook

        injector, persister = create_queue_hook(queue=queue)
        team = self._session.main_team
        existing_tool_hooks = team.tool_hooks or []
        team.tool_hooks = [*existing_tool_hooks, injector]
        existing_post_hooks = team.post_hooks or []
        team.post_hooks = [*existing_post_hooks, persister]

    def wire_orchestrate_progress(self, callback) -> None:
        """Set a progress callback on the orchestrate tool."""
        from ember_code.core.tools.orchestrate import OrchestrateTools

        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                tool._on_progress = callback
                break

    @staticmethod
    async def _close_model_http_client(team: Any) -> None:
        """Close the httpx client on the model to release open HTTP streams.

        When an Agno run finishes or is cancelled mid-stream, the underlying
        httpx connection to the API may stay open indefinitely. Closing the
        client ensures the TCP connection is torn down promptly so the server
        can release concurrency slots. A fresh client is assigned so the model
        remains usable for subsequent runs.
        """
        import httpx as _httpx

        try:
            model = getattr(team, "model", None)
            client = getattr(model, "http_client", None) if model else None
            if isinstance(client, _httpx.AsyncClient):
                await asyncio.wait_for(client.aclose(), timeout=3)
        except Exception as exc:
            logger.debug("Failed to close model HTTP client: %s", exc)

        # Always ensure a fresh client, even if close failed.
        # The old client's connections will eventually timeout.
        if model is not None:
            model.http_client = _httpx.AsyncClient(
                limits=_httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30,
                ),
            )

    def cancel_run(self) -> None:
        """Cancel the currently running agent and kill any foreground process."""
        # Kill the active foreground subprocess first so the blocking
        # tool call returns quickly and the Agno cancellation can fire.
        from ember_code.core.tools.shell import cancel_foreground

        if cancel_foreground():
            logger.info("Killed foreground process on cancel")

        try:
            from agno.agent import Agent

            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)

    @property
    def processing(self) -> bool:
        return self._processing

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def run_timeout(self) -> int:
        return self._settings.models.max_run_timeout

    @property
    def skill_names(self) -> list[str]:
        """Skill names for input autocomplete — FE needs these for the input handler."""
        return [s.name for s in self._session.skill_pool.list_skills()]

    def get_skill_pool(self):
        """Return the skill pool for input autocomplete."""
        return self._session.skill_pool

    def get_mcp_server_details(self) -> list[dict]:
        """Full MCP server info for the panel UI."""
        mgr = self._session.mcp_manager
        servers = []
        for name in mgr.list_servers():
            config = mgr.configs.get(name)
            connected = name in mgr.list_connected()
            servers.append(
                {
                    "name": name,
                    "connected": connected,
                    "transport": config.type if config else "unknown",
                    "tool_names": mgr.get_tools(name),
                    "tool_descriptions": mgr.get_tool_descriptions(name),
                    "error": mgr.get_error(name),
                    "policy_blocked": mgr._policy.is_denied(name),
                }
            )
        return servers

    async def get_chat_history(self, session_id: str) -> list[dict]:
        """Get chat history for a session. Returns list of {role, content} dicts."""
        agent = self._session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        if agno_session is None:
            return []
        messages = agno_session.get_chat_history()
        if not messages:
            return []
        return [
            {
                "role": msg.role,
                "content": msg.content if isinstance(msg.content, str) else str(msg.content or ""),
            }
            for msg in messages
        ]

    def get_mcp_servers(self) -> list[dict]:
        """MCP server info for the panel."""
        mgr = self._session.mcp_manager
        servers = []
        for name in mgr.list_servers():
            connected = name in mgr.list_connected()
            servers.append({"name": name, "connected": connected})
        return servers

    async def mcp_connect(self, server_name: str) -> msg.Info:
        """Connect a single MCP server."""
        await self._session.mcp_manager.connect(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Connected MCP: {server_name}")

    async def mcp_disconnect(self, server_name: str) -> msg.Info:
        """Disconnect a single MCP server."""
        await self._session.mcp_manager.disconnect_one(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Disconnected MCP: {server_name}")

    # ── Agents ─────────────────────────────────────────────────────

    def get_agent_details(self) -> list[AgentInfo]:
        """Snapshot of every loaded agent for the panel UI.

        Combines :meth:`AgentPool.list_agents` with the ephemeral
        directory check so the panel can render the "ephemeral" badge
        + show the promote/discard actions without making a second
        RPC call. Includes the full ``system_prompt`` since the panel
        expands it inline on Enter.
        """
        from ember_code.core.pool import AgentInfo

        pool = self._session.pool
        ephemeral_dir = getattr(pool, "_ephemeral_dir", None)
        results: list[AgentInfo] = []
        for defn in pool.list_agents():
            is_ephemeral = bool(
                ephemeral_dir and defn.source_path and ephemeral_dir in defn.source_path.parents
            )
            results.append(
                AgentInfo(
                    name=defn.name,
                    description=defn.description,
                    tools=list(defn.tools),
                    model=defn.model or "",
                    color=defn.color or "",
                    can_orchestrate=defn.can_orchestrate,
                    mcp_servers=list(defn.mcp_servers),
                    tags=list(defn.tags),
                    system_prompt=defn.system_prompt,
                    source_path=str(defn.source_path) if defn.source_path else "",
                    is_ephemeral=is_ephemeral,
                )
            )
        return results

    def promote_ephemeral_agent(self, name: str) -> msg.Info:
        """Save an ephemeral agent permanently (called from the panel)."""
        try:
            dest = self._session.pool.promote_ephemeral(name, self._session.project_dir)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Promoted '{name}' to {dest}")

    def discard_ephemeral_agent(self, name: str) -> msg.Info:
        """Delete an ephemeral agent (called from the panel)."""
        try:
            self._session.pool.discard_ephemeral(name)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Discarded ephemeral agent '{name}'.")

    # ── Skills ─────────────────────────────────────────────────────

    # ── Knowledge ──────────────────────────────────────────────────

    async def get_knowledge_status(self) -> dict:
        """Status snapshot for the knowledge panel header.

        Returns a plain dict (vs a typed model) because
        ``KnowledgeStatus`` already serializes cleanly and this is a
        read-only display payload — no behavior depends on the type."""
        status = await self._session.knowledge_mgr.status()
        return {
            "enabled": status.enabled,
            "collection_name": status.collection_name,
            "document_count": status.document_count,
            "embedder": status.embedder,
        }

    async def knowledge_search(self, query: str) -> list[dict]:
        """Search the knowledge base. Returns one dict per hit with
        ``name``, ``content``, ``score``, ``metadata`` — same shape
        the panel reconstructs into ``KnowledgeSearchHit``."""
        response = await self._session.knowledge_mgr.search(query)
        return [
            {
                "name": r.name,
                "content": r.content,
                "score": r.score,
                "metadata": dict(r.metadata),
            }
            for r in response.results
        ]

    async def knowledge_add(self, source: str) -> msg.Info:
        """Add content to the knowledge base from the panel. Dispatch
        rules mirror ``/knowledge add <source>``: HTTP URLs → URL
        ingest, path-shaped strings → file/dir ingest, anything else
        → inline text."""
        mgr = self._session.knowledge_mgr
        if source.startswith(("http://", "https://")):
            result = await mgr.add_url(source)
        elif "/" in source or source.startswith("."):
            result = await mgr.add_path(source)
        else:
            result = await mgr.add(text=source)
        if not result.success:
            return msg.Info(text=result.error or "Add failed.")
        return msg.Info(text=result.message)

    # ── Skills ─────────────────────────────────────────────────────

    def get_skill_details(self) -> list[SkillInfo]:
        """Snapshot of every loaded skill for the panel UI.

        Sends the full ``body`` (which the panel head-clips for the
        expanded view) so toggling expansion doesn't need an extra
        RPC round trip per row.
        """
        from ember_code.core.skills.parser import SkillInfo

        return [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                version=skill.version,
                category=skill.category,
                argument_hint=skill.argument_hint,
                context=skill.context,
                agent=skill.agent or "",
                user_invocable=skill.user_invocable,
                body=skill.body,
                source_dir=str(skill.source_dir) if skill.source_dir else "",
            )
            for skill in self._session.skill_pool.list_skills()
        ]

    # ── Plugins ────────────────────────────────────────────────────

    def get_plugin_details(self) -> list[PluginInfo]:
        """Snapshot of every discovered plugin for the panel UI.

        Combines :class:`PluginLoader` discovery state with the
        persisted enable/disable list and pinned-SHA map so the panel
        can render counts, version, source root, and toggle status
        without any further RPC chatter. Returns typed
        :class:`PluginInfo` models — the wire format is defined in
        :mod:`core.plugins.models` so backend and frontend share the
        same shape.
        """
        from ember_code.core.plugins.models import PluginInfo

        loader = self._session.plugin_loader
        state = self._session.plugin_state
        disabled = set(state.disabled)
        return [
            PluginInfo(
                name=p.name,
                version=p.manifest.version or "",
                description=p.manifest.description or "",
                source_root=p.source.root,
                path=str(p.root_path),
                enabled=p.name not in disabled,
                has_skills=p.has_skills,
                has_agents=p.has_agents,
                has_hooks=p.has_hooks,
                has_mcp=p.has_mcp,
                has_tools=p.has_tools,
                pin=state.pins.get(p.name, ""),
            )
            for p in loader.list_plugins()
        ]

    def set_plugin_enabled(self, name: str, enabled: bool) -> msg.Info:
        """Toggle a plugin's enabled flag and persist. Restart hint
        included in the return text — the change takes effect on next
        session start since hot-reload across all five extension types
        is non-trivial."""
        from ember_code.core.plugins.state import save_state

        loader = self._session.plugin_loader
        state = self._session.plugin_state
        if loader.get(name) is None:
            return msg.Info(text=f"No plugin named '{name}'.")

        disabled_set = set(state.disabled)
        if enabled:
            disabled_set.discard(name)
        else:
            disabled_set.add(name)
        state.disabled = sorted(disabled_set)
        save_state(state, data_dir=self._session.settings.storage.data_dir)

        verb = "enabled" if enabled else "disabled"
        return msg.Info(text=f"Plugin '{name}' {verb}. Restart the session to apply.")

    def install_plugin(self, ref: str, install_ref: str | None = None) -> msg.Info:
        """Install a plugin by git URL or ``@<marketplace>/<plugin>`` ref.

        ``install_ref`` (the ``--ref`` flag in the slash command — a
        branch / tag / SHA) is forwarded to the installer. Marketplace
        refs may carry a default ``branch`` in the catalog; honored
        only when ``install_ref`` is omitted so explicit user choice
        wins.
        """
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )
        from ember_code.core.plugins.marketplaces import resolve_install_ref

        data_dir = self._session.settings.storage.data_dir
        installer = PluginInstaller(data_dir=data_dir)
        if not installer.is_git_available():
            return msg.Info(text="git not found on PATH. Install git and retry.")

        url = ref
        if ref.startswith("@"):
            resolved = resolve_install_ref(ref, data_dir=data_dir)
            if resolved is None:
                return msg.Info(
                    text=f"Could not resolve '{ref}'. Run "
                    "/plugin marketplace list to see registered "
                    "marketplaces, or use a git URL."
                )
            url, mkt_entry = resolved
            if install_ref is None and mkt_entry.branch:
                install_ref = mkt_entry.branch

        try:
            manifest = installer.install(url, ref=install_ref)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except PluginError as e:
            return msg.Info(text=str(e))

        version = f" v{manifest.version}" if manifest.version else ""
        return msg.Info(
            text=f"Installed plugin '{manifest.name}'{version}. Restart the session to activate."
        )

    def update_plugin(self, name: str, install_ref: str | None = None) -> msg.Info:
        """Fetch + reset to ``install_ref`` (default: origin's HEAD)."""
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )

        installer = PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
        )
        if not installer.is_git_available():
            return msg.Info(text="git not found on PATH.")
        try:
            new_sha = installer.update(name, ref=install_ref)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except PluginError as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Updated '{name}' to {new_sha[:12]}. Restart to apply.")

    def remove_plugin(self, name: str) -> msg.Info:
        """Delete the plugin directory and clear its pin."""
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )

        installer = PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
        )
        try:
            installer.remove(name)
        except PluginError as e:
            return msg.Info(text=str(e))
        return msg.Info(
            text=f"Removed '{name}'. Restart the session to drop its skills/agents/hooks/MCP/tools."
        )

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        """Snapshot of every registered marketplace for the panel.

        Returns typed :class:`MarketplaceInfo` models (nesting
        :class:`MarketplacePluginInfo` per catalog entry). Same wire
        contract as ``get_plugin_details`` — source-of-truth shape
        lives in :mod:`core.plugins.models`.
        """
        from ember_code.core.plugins.marketplaces import load_registry
        from ember_code.core.plugins.models import (
            MarketplaceInfo,
            MarketplacePluginInfo,
        )

        registry = load_registry(
            data_dir=self._session.settings.storage.data_dir,
        )
        return [
            MarketplaceInfo(
                name=m.name,
                url=m.url,
                last_fetched=m.last_fetched or "",
                plugins=[
                    MarketplacePluginInfo(
                        name=p.name,
                        source=p.source,
                        description=p.description or "",
                        version=p.version or "",
                        branch=p.branch or "",
                    )
                    for p in (m.cached.plugins if m.cached else [])
                ],
            )
            for m in registry.marketplaces
        ]

    def add_marketplace(self, url: str) -> msg.Info:
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.marketplaces import (
            add_marketplace as _add,
        )

        try:
            entry = _add(url, data_dir=self._session.settings.storage.data_dir)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except Exception as e:  # noqa: BLE001 — surface verbatim
            return msg.Info(text=f"Failed to add marketplace: {e}")
        count = len(entry.cached.plugins) if entry.cached else 0
        return msg.Info(text=f"Added '{entry.name}' ({count} plugin(s) catalogued).")

    def remove_marketplace(self, name: str) -> msg.Info:
        from ember_code.core.plugins.marketplaces import (
            remove_marketplace as _remove,
        )

        if not _remove(name, data_dir=self._session.settings.storage.data_dir):
            return msg.Info(text=f"No marketplace named '{name}'.")
        return msg.Info(text=f"Unregistered '{name}'. Installed plugins from it remain.")

    def refresh_marketplaces(self, name: str | None = None) -> msg.Info:
        """Re-fetch one marketplace or all. Errors per-marketplace are
        collected and reported together so a single bad URL doesn't
        abort the whole refresh."""
        from ember_code.core.plugins.marketplaces import (
            load_registry,
        )
        from ember_code.core.plugins.marketplaces import (
            refresh_marketplace as _refresh,
        )

        data_dir = self._session.settings.storage.data_dir
        if name:
            try:
                entry = _refresh(name, data_dir=data_dir)
            except Exception as e:  # noqa: BLE001
                return msg.Info(text=f"Refresh failed for '{name}': {e}")
            if entry is None:
                return msg.Info(text=f"No marketplace named '{name}'.")
            count = len(entry.cached.plugins) if entry.cached else 0
            return msg.Info(text=f"Refreshed '{entry.name}' ({count} plugins).")

        registry = load_registry(data_dir=data_dir)
        ok: list[str] = []
        failed: list[str] = []
        for m in registry.marketplaces:
            try:
                _refresh(m.name, data_dir=data_dir)
                ok.append(m.name)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{m.name} ({e})")
        if not ok and not failed:
            return msg.Info(text="No marketplaces to refresh.")
        line = f"Refreshed {len(ok)} ok"
        if failed:
            line += f"; {len(failed)} failed: {', '.join(failed)}"
        return msg.Info(text=line)

    async def fire_session_start_hook(self) -> None:
        """Fire the SessionStart hook."""
        from ember_code.core.hooks.events import HookEvent

        with contextlib.suppress(Exception):
            await self._session.hook_executor.execute(
                event=HookEvent.SESSION_START.value,
                payload={"session_id": self._session.session_id},
            )

    async def auto_sync_knowledge(self) -> str | None:
        """Auto-sync knowledge file on startup. Returns status message or None."""
        if self._session.knowledge is None:
            return None
        try:
            result = await self._session.knowledge_mgr.sync_from_file()
            if result:
                return f"Knowledge synced: {result}"
        except Exception:
            pass
        return None

    async def execute_scheduled_task(self, description: str) -> str:
        """Execute a scheduled task via the agent. Returns result text."""
        from ember_code.core.utils.response import extract_response_text

        team = self._session.main_team
        response = await team.arun(description, stream=False)
        return extract_response_text(response)

    async def cancel_scheduled_task(self, task_id: str) -> msg.Info:
        """Cancel a scheduled task."""
        from ember_code.core.scheduler.models import TaskStatus
        from ember_code.core.scheduler.store import TaskStore

        store = TaskStore()
        await store.update_status(task_id, TaskStatus.cancelled)
        return msg.Info(text=f"Cancelled task {task_id}")

    async def get_scheduled_tasks(self, include_done: bool = True) -> list:
        """Get all scheduled tasks."""
        from ember_code.core.scheduler.store import TaskStore

        store = TaskStore()
        return await store.get_all(include_done=include_done)

    def start_scheduler(
        self,
        on_task_started=None,
        on_task_completed=None,
    ) -> Any:
        """Start the background scheduler. Returns the runner for stop()."""
        from ember_code.core.scheduler.runner import SchedulerRunner
        from ember_code.core.scheduler.store import TaskStore

        sched_cfg = self._settings.scheduler
        store = TaskStore()
        runner = SchedulerRunner(
            store=store,
            execute_fn=self.execute_scheduled_task,
            on_task_started=on_task_started,
            on_task_completed=on_task_completed,
            poll_interval=sched_cfg.poll_interval,
            task_timeout=sched_cfg.task_timeout,
            max_concurrent=sched_cfg.max_concurrent,
        )
        runner.start()
        return runner

    def toggle_verbose(self) -> bool:
        """Toggle verbose mode. Returns new state."""
        self._settings.display.show_routing = not self._settings.display.show_routing
        return self._settings.display.show_routing
