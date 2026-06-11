"""BackendClient — FE-side proxy that communicates with BackendServer over Unix socket.

Exposes the same interface as BackendServer so all FE code
(RunController, SessionManager, App) works unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from ember_code.protocol import messages as msg
from ember_code.protocol.messages import Message
from ember_code.protocol.rpc import RpcMethod
from ember_code.transport.unix_socket import UnixSocketClientTransport

logger = logging.getLogger(__name__)


class RemoteSkillPool:
    """Thin wrapper over serialized skill definitions for autocomplete."""

    def __init__(self, definitions: list[dict]):
        self._definitions = definitions

    def list_skills(self) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(**d) for d in self._definitions]

    def match_user_command(self, text: str) -> Any | None:
        return None  # Commands handled on BE


class BackendClient:
    """FE-side proxy for BackendServer over Unix socket.

    Provides the same public interface as BackendServer.
    All calls are serialized to protocol messages and sent over the socket.
    """

    def __init__(self, socket_path: str):
        self._socket_path = socket_path
        self._transport = UnixSocketClientTransport(socket_path)
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_streams: dict[str, asyncio.Queue] = {}
        self._push_handlers: dict[str, Callable] = {}
        self._reader_task: asyncio.Task | None = None
        self._connected = False
        # Mirroring (multi-view sessions): uncorrelated events like
        # remote Typing / UserMessageReceived land here instead of
        # the "Unmatched message" debug log.
        self._mirror_handler: Callable[[Message], None] | None = None
        self._typing_pending: str | None = None
        self._typing_timer: Any = None

        # Cached sync properties
        self._cached_processing = False
        self._cached_session_id = ""
        self._cached_run_timeout = 300
        self._cached_skill_names: list[str] = []
        self._cached_skill_pool: RemoteSkillPool | None = None
        self._cached_settings: Any = None
        self._cached_status: msg.StatusUpdate = msg.StatusUpdate()

    # ── Connection ────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the BE socket and start the reader loop."""
        await self._transport.connect(timeout=30.0)
        self._connected = True
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Background task: read messages from BE, dispatch by correlation ID."""
        try:
            async for message in self._transport.receive():
                msg_id = message.id

                # StreamEnd → signal end of streaming response
                if isinstance(message, msg.StreamEnd):
                    queue = self._pending_streams.pop(msg_id, None)
                    if queue:
                        await queue.put(None)  # sentinel
                    continue

                # Push notification → dispatch to handler
                if isinstance(message, msg.PushNotification):
                    handler = self._push_handlers.get(message.channel)
                    if handler:
                        try:
                            handler(message.payload)
                        except Exception as exc:
                            logger.debug("Push handler error (%s): %s", message.channel, exc)
                    continue

                # Streaming response → put in queue
                if msg_id and msg_id in self._pending_streams:
                    await self._pending_streams[msg_id].put(message)
                    continue

                # Request/response → resolve future
                if msg_id and msg_id in self._pending:
                    self._pending.pop(msg_id).set_result(message)
                    continue

                # Mirroring events from other attached views (web
                # tabs on the same BE): live drafts, message echoes,
                # remote HITL resolutions.
                if isinstance(
                    message,
                    (msg.Typing, msg.UserMessageReceived, msg.RequirementResolved, msg.Welcome),
                ):
                    if self._mirror_handler is not None:
                        try:
                            self._mirror_handler(message)
                        except Exception as exc:
                            logger.debug("Mirror handler error: %s", exc)
                    continue

                logger.debug("Unmatched message: %s (id=%s)", type(message).__name__, msg_id)

        except Exception as exc:
            logger.error("Reader loop error: %s", exc)
            # Fail all pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("BE connection lost"))
            for queue in self._pending_streams.values():
                await queue.put(None)

    async def _rpc(self, method: RpcMethod, **args: Any) -> Any:
        """Send an RPC request and wait for the response.

        ``method`` is the :class:`RpcMethod` enum member — its string
        value goes on the wire. Typing it as the enum (not ``str``)
        means typos and renames surface at the call site, not as a
        runtime ``Unknown RPC method`` error.
        """
        req_id = uuid.uuid4().hex[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        # ``method.value`` is the wire string; ``RPCRequest.method`` is
        # typed ``str`` on the protocol so we pass the .value explicitly
        # rather than relying on StrEnum's str-ness.
        await self._transport.send(msg.RPCRequest(id=req_id, method=method.value, args=args))
        response = await asyncio.wait_for(future, timeout=60.0)
        if isinstance(response, msg.RPCResponse):
            if response.error:
                raise RuntimeError(response.error)
            return response.result
        # Direct message response (e.g., Info, StatusUpdate)
        return response

    def set_mirror_handler(self, handler: Callable[[Message], None]) -> None:
        """Receive mirroring events from other views on the same BE."""
        self._mirror_handler = handler

    def notify_typing(self, text: str) -> None:
        """Broadcast this view's live composer draft (mirroring).

        Trailing-edge throttled to ~10/s; the final value always
        flushes so other views never display a stale draft. Fire and
        forget — drafts are cosmetic, drops are fine.
        """
        if not self._connected:
            return
        self._typing_pending = text

        if self._typing_timer is not None:
            return

        def _flush() -> None:
            self._typing_timer = None
            pending = self._typing_pending
            self._typing_pending = None
            if pending is None:
                return
            asyncio.ensure_future(self._transport.send(msg.Typing(text=pending, client_id="tui")))

        self._typing_timer = asyncio.get_event_loop().call_later(0.1, _flush)

    async def _send_and_wait(self, message: Message) -> Message:
        """Send a typed message and wait for one response."""
        req_id = uuid.uuid4().hex[:8]
        message = message.model_copy(update={"id": req_id})
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._transport.send(message)
        return await asyncio.wait_for(future, timeout=60.0)

    async def _stream(self, message: Message) -> AsyncIterator[Message]:
        """Send a message and yield streaming responses until StreamEnd."""
        req_id = uuid.uuid4().hex[:8]
        message = message.model_copy(update={"id": req_id})
        queue: asyncio.Queue = asyncio.Queue()
        self._pending_streams[req_id] = queue
        await self._transport.send(message)
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    # ── Refresh cached state ──────────────────────────────────────

    async def refresh_cache(self) -> None:
        """Fetch initial state from BE after connecting."""
        self._cached_session_id = await self._rpc(RpcMethod.GET_SESSION_ID)
        self._cached_run_timeout = await self._rpc(RpcMethod.GET_RUN_TIMEOUT)
        self._cached_skill_names = await self._rpc(RpcMethod.GET_SKILL_NAMES) or []
        skill_defs = await self._rpc(RpcMethod.GET_SKILL_DEFINITIONS)
        self._cached_skill_pool = RemoteSkillPool(skill_defs or [])
        # Cache status for sync get_status() calls
        status_data = await self._rpc(RpcMethod.GET_STATUS)
        if isinstance(status_data, dict):
            self._cached_status = msg.StatusUpdate(**status_data)
        elif isinstance(status_data, msg.StatusUpdate):
            self._cached_status = status_data
        else:
            self._cached_status = msg.StatusUpdate()

    # ── Streaming methods ────────────────────────────────────────

    async def run_message(self, text: str, media: dict | None = None) -> AsyncIterator[Message]:
        self._cached_processing = True
        try:
            async for proto in self._stream(msg.UserMessage(text=text, file_contents=media or {})):
                yield proto
        finally:
            self._cached_processing = False

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[Message]:
        async for proto in self._stream(
            msg.HITLResponse(requirement_id=requirement_id, action=action, choice=choice)
        ):
            yield proto

    async def resolve_hitl_batch(
        self, decisions: list[tuple[str, str, str]]
    ) -> AsyncIterator[Message]:
        """Resolve every requirement from a multi-req pause in one round-trip.

        ``decisions`` is a list of ``(requirement_id, action, choice)``
        tuples. The backend confirms/rejects each one in Agno's
        internal state and then calls ``acontinue_run`` ONCE with the
        full resolved set — fixing the silent-denial bug where a
        per-req loop only resolved the first call of a batched tool
        plan.
        """
        batch = msg.HITLResponseBatch(
            decisions=[
                msg.HITLDecision(requirement_id=rid, action=a, choice=c)
                for (rid, a, c) in decisions
            ]
        )
        async for proto in self._stream(batch):
            yield proto

    # ── Command ──────────────────────────────────────────────────

    async def handle_command(self, text: str) -> msg.CommandResult:
        result = await self._send_and_wait(msg.Command(text=text))
        if isinstance(result, msg.CommandResult):
            return result
        return msg.CommandResult(
            kind=msg.CommandResultKind.ERROR,
            content=str(result),
            action=msg.CommandAction.NONE,
        )

    # ── Session management ───────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        result = await self._send_and_wait(msg.SessionList())
        if isinstance(result, msg.SessionListResult):
            return result
        return msg.SessionListResult()

    async def switch_session(self, session_id: str) -> msg.Info:
        result = await self._send_and_wait(msg.SessionSwitch(session_id=session_id))
        self._cached_session_id = session_id
        return result

    async def get_chat_history(self, session_id: str) -> list[dict]:
        return await self._rpc(RpcMethod.GET_CHAT_HISTORY, session_id=session_id) or []

    async def get_pending_messages(self, session_id: str) -> list[dict]:
        """User messages whose runs never completed — surfaced on
        ``--continue`` so the interrupted prompt renders alongside
        normal chat history. See ``BackendServer.get_pending_messages``
        for the persistence model."""
        return await self._rpc(RpcMethod.GET_PENDING_MESSAGES, session_id=session_id) or []

    # ── Model ────────────────────────────────────────────────────

    async def switch_model(self, model_name: str) -> msg.Info:
        """Switch the active model on the BE, wait for confirmation,
        and refresh the cached status so the FE status bar reads
        the new model name.

        Two changes from the v0.5.13 implementation:

        1. Awaits the RPC instead of fire-and-forget. The earlier
           version returned ``Info`` immediately while the BE was
           still processing, so any follow-up
           ``update_status_bar`` raced the switch and rendered the
           OLD model.
        2. Re-fetches ``GET_STATUS`` and updates ``_cached_status``.
           ``get_status()`` is a cache read by design (it's called
           on every status-bar tick), and the cache is only
           populated at session start via ``refresh_cache``. Without
           the explicit refresh here, the status bar reads the
           stale boot-time model long after the switch.
        """
        result = await self._send_and_wait(msg.ModelSwitch(model_name=model_name))
        await self._refresh_cached_status()
        if isinstance(result, msg.Info):
            return result
        return msg.Info(text=f"Switched to {model_name}")

    async def _refresh_cached_status(self) -> None:
        """Re-fetch ``GET_STATUS`` from the BE into the local cache.

        The cache backs the sync ``get_status()`` accessor used by
        the status-bar tick. Any operation that changes BE-side
        status fields (model, cloud auth, session id) must call
        this to keep the cache fresh — otherwise the next render
        pulls stale fields.
        """
        try:
            status_data = await self._rpc(RpcMethod.GET_STATUS)
        except Exception as exc:
            logging.getLogger(__name__).debug("status refresh failed: %s", exc)
            return
        if isinstance(status_data, dict):
            self._cached_status = msg.StatusUpdate(**status_data)
        elif isinstance(status_data, msg.StatusUpdate):
            self._cached_status = status_data

    # ── MCP ──────────────────────────────────────────────────────

    async def ensure_mcp(self) -> None:
        await self._rpc(RpcMethod.ENSURE_MCP)

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        result = await self._send_and_wait(msg.MCPToggle(server_name=server_name, connect=connect))
        return result

    async def mcp_connect(self, server_name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.MCP_CONNECT, server_name=server_name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def mcp_disconnect(self, server_name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.MCP_DISCONNECT, server_name=server_name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    def get_mcp_status(self) -> list[tuple[str, bool]]:
        # Sync call — use cached or fire async
        fut = asyncio.ensure_future(self._rpc(RpcMethod.GET_MCP_STATUS))
        try:
            if fut.done():
                return fut.result() or []
        except Exception:
            pass
        return []

    def get_mcp_server_details(self) -> list[dict]:
        fut = asyncio.ensure_future(self._rpc(RpcMethod.GET_MCP_SERVER_DETAILS))
        try:
            if fut.done():
                return fut.result() or []
        except Exception:
            pass
        return []

    # ── Agents ─────────────────────────────────────────────────────

    async def get_agent_details(self) -> list[dict]:
        result = await self._rpc(RpcMethod.GET_AGENT_DETAILS)
        return result or []

    async def promote_ephemeral_agent(self, name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT, name=name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def discard_ephemeral_agent(self, name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT, name=name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    # ── Skills ─────────────────────────────────────────────────────

    async def get_skill_details(self) -> list[dict]:
        result = await self._rpc(RpcMethod.GET_SKILL_DETAILS)
        return result or []

    # ── Hooks ──────────────────────────────────────────────────────

    async def get_hooks_details(self) -> list[dict]:
        result = await self._rpc(RpcMethod.GET_HOOKS_DETAILS)
        return result or []

    async def reload_hooks(self) -> msg.Info:
        result = await self._rpc(RpcMethod.RELOAD_HOOKS)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    # ── Knowledge ──────────────────────────────────────────────────

    async def get_knowledge_status(self) -> dict:
        result = await self._rpc(RpcMethod.GET_KNOWLEDGE_STATUS)
        return result or {}

    async def knowledge_search(self, query: str) -> list[dict]:
        result = await self._rpc(RpcMethod.KNOWLEDGE_SEARCH, query=query)
        return result or []

    async def knowledge_add(self, source: str) -> msg.Info:
        result = await self._rpc(RpcMethod.KNOWLEDGE_ADD, source=source)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    # ── CodeIndex ──────────────────────────────────────────────────

    async def codeindex_status(self) -> dict:
        result = await self._rpc(RpcMethod.CODEINDEX_STATUS)
        return result or {}

    async def codeindex_sync(self, sha: str | None = None) -> dict:
        result = await self._rpc(RpcMethod.CODEINDEX_SYNC, sha=sha)
        return result or {}

    async def codeindex_resync(self, sha: str | None = None) -> dict:
        result = await self._rpc(RpcMethod.CODEINDEX_RESYNC, sha=sha)
        return result or {}

    async def codeindex_clean(self) -> dict:
        result = await self._rpc(RpcMethod.CODEINDEX_CLEAN)
        return result or {}

    async def codeindex_install(self) -> dict:
        result = await self._rpc(RpcMethod.CODEINDEX_INSTALL)
        return result or {}

    # ── Plugins ─────────────────────────────────────────────────────

    async def get_plugin_details(self) -> list[dict]:
        result = await self._rpc(RpcMethod.GET_PLUGIN_DETAILS)
        return result or []

    async def set_plugin_enabled(self, name: str, enabled: bool) -> msg.Info:
        result = await self._rpc(
            RpcMethod.SET_PLUGIN_ENABLED,
            name=name,
            enabled=enabled,
        )
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def install_plugin(
        self,
        ref: str,
        install_ref: str | None = None,
    ) -> msg.Info:
        result = await self._rpc(
            RpcMethod.INSTALL_PLUGIN,
            ref=ref,
            install_ref=install_ref,
        )
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def update_plugin(
        self,
        name: str,
        install_ref: str | None = None,
    ) -> msg.Info:
        result = await self._rpc(
            RpcMethod.UPDATE_PLUGIN,
            name=name,
            install_ref=install_ref,
        )
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def remove_plugin(self, name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.REMOVE_PLUGIN, name=name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def get_marketplaces(self) -> list[dict]:
        result = await self._rpc(RpcMethod.GET_MARKETPLACES)
        return result or []

    async def add_marketplace(self, url: str) -> msg.Info:
        result = await self._rpc(RpcMethod.ADD_MARKETPLACE, url=url)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def remove_marketplace(self, name: str) -> msg.Info:
        result = await self._rpc(RpcMethod.REMOVE_MARKETPLACE, name=name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    async def refresh_marketplaces(
        self,
        name: str | None = None,
    ) -> msg.Info:
        result = await self._rpc(RpcMethod.REFRESH_MARKETPLACES, name=name)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result))

    def get_mcp_servers(self) -> list[dict]:
        fut = asyncio.ensure_future(self._rpc(RpcMethod.GET_MCP_SERVERS))
        try:
            if fut.done():
                return fut.result() or []
        except Exception:
            pass
        return []

    # ── Status ───────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        return getattr(self, "_cached_status", msg.StatusUpdate())

    # ── Cloud auth ───────────────────────────────────────────────

    async def start_login(self) -> None:
        """Tell the BE to start the login flow.

        Status and results arrive via push notifications (login_status,
        login_result) — handled by app-level push handlers.
        """
        await self._rpc(RpcMethod.LOGIN)

    def cancel_login(self) -> None:
        """Tell the BE to cancel the in-progress login flow."""
        from ember_code.protocol import messages as msg

        asyncio.ensure_future(self._transport.send(msg.CancelLogin()))

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        fut = asyncio.ensure_future(self._rpc(RpcMethod.RELOAD_CLOUD_CREDENTIALS))
        try:
            if fut.done():
                result = fut.result()
                if isinstance(result, msg.StatusUpdate):
                    return result
                if isinstance(result, dict):
                    return msg.StatusUpdate(**result)
        except Exception:
            pass
        return msg.StatusUpdate()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        fut = asyncio.ensure_future(self._rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS))
        try:
            if fut.done():
                result = fut.result()
                if isinstance(result, msg.StatusUpdate):
                    return result
                if isinstance(result, dict):
                    return msg.StatusUpdate(**result)
        except Exception:
            pass
        return msg.StatusUpdate()

    # ── /loop continuation ───────────────────────────────────────

    async def pop_pending_loop_iteration(self) -> dict | None:
        """RPC: pop the next ``/loop`` iteration's descriptor.

        Returns ``{"prompt": str, "iteration": int, "remaining": int}``
        when an iteration is queued, ``None`` when no loop is active.
        The backend decrements its iteration counter as part of this
        call so consecutive callers can't double-fire.
        """
        result = await self._rpc(RpcMethod.POP_PENDING_LOOP_ITERATION)
        if isinstance(result, dict) and isinstance(result.get("prompt"), str):
            return result
        return None

    async def cancel_pending_loop(self) -> bool:
        """RPC: clear ``/loop`` state on the backend. Returns ``True``
        if a loop was actually cancelled."""
        result = await self._rpc(RpcMethod.CANCEL_PENDING_LOOP)
        return bool(result)

    async def loop_status(self) -> dict:
        """RPC: snapshot the active ``/loop`` state for the panel
        header. Cheap (just three session fields); safe to poll."""
        result = await self._rpc(RpcMethod.LOOP_STATUS)
        return result or {}

    async def loop_resume(self) -> str:
        """RPC: flip a paused loop to pumping. Returns the prompt
        verbatim so the caller can fire it via ``_run`` directly
        (bypassing the cancel guard). Empty string when nothing to
        resume."""
        result = await self._rpc(RpcMethod.LOOP_RESUME)
        return result or ""

    async def loop_pause(self) -> bool:
        """RPC: pause the active loop without advancing the counter.

        Called by ``_check_loop_continuation`` when an iteration's
        ``_run`` raised — the failed iteration stays at its
        current index so a subsequent resume retries it."""
        result = await self._rpc(RpcMethod.LOOP_PAUSE)
        return bool(result)

    # ── Compaction / Learning ────────────────────────────────────

    async def count_context_tokens(self) -> int:
        result = await self._rpc(RpcMethod.COUNT_CONTEXT_TOKENS)
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            return 0

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        result = await self._rpc(
            RpcMethod.COMPACT_IF_NEEDED, ctx_tokens=ctx_tokens, max_ctx=max_ctx
        )
        if result is None or result is False:
            return None
        if isinstance(result, msg.SessionCleared):
            return result
        if isinstance(result, dict):
            return msg.SessionCleared(**result)
        return None

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        await self._rpc(RpcMethod.EXTRACT_LEARNINGS, user_msg=user_msg, assistant_msg=assistant_msg)

    # ── Knowledge ────────────────────────────────────────────────

    async def auto_sync_knowledge(self) -> str | None:
        return await self._rpc(RpcMethod.AUTO_SYNC_KNOWLEDGE)

    # ── Hooks ────────────────────────────────────────────────────

    async def fire_session_start_hook(self) -> None:
        await self._rpc(RpcMethod.FIRE_SESSION_START_HOOK)

    # ── Scheduler ────────────────────────────────────────────────

    def start_scheduler(
        self,
        on_task_started: Callable | None = None,
        on_task_completed: Callable | None = None,
    ) -> None:
        if on_task_started:
            self._push_handlers["scheduler_started"] = lambda p: on_task_started(
                p.get("task_id", ""), p.get("description", "")
            )
        if on_task_completed:
            self._push_handlers["scheduler_completed"] = lambda p: on_task_completed(
                p.get("task_id", ""), p.get("description", ""), p.get("result", "")
            )
        asyncio.ensure_future(self._rpc(RpcMethod.START_SCHEDULER))

    async def execute_scheduled_task(self, description: str) -> str:
        return await self._rpc(RpcMethod.EXECUTE_SCHEDULED_TASK, description=description) or ""

    async def cancel_scheduled_task(self, task_id: str) -> msg.Info:
        result = await self._rpc(RpcMethod.CANCEL_SCHEDULED_TASK, task_id=task_id)
        return result if isinstance(result, msg.Info) else msg.Info(text=str(result or ""))

    async def get_scheduled_tasks(self, include_done: bool = True) -> list:
        from types import SimpleNamespace

        tasks = await self._rpc(RpcMethod.GET_SCHEDULED_TASKS, include_done=include_done) or []
        return [SimpleNamespace(**t) if isinstance(t, dict) else t for t in tasks]

    # ── Sync properties (cached) ─────────────────────────────────

    @property
    def processing(self) -> bool:
        return self._cached_processing

    @property
    def session_id(self) -> str:
        return self._cached_session_id

    @property
    def settings(self) -> Any:
        return self._cached_settings

    @property
    def run_timeout(self) -> int:
        return self._cached_run_timeout

    @property
    def skill_names(self) -> list[str]:
        return self._cached_skill_names

    def get_skill_pool(self) -> RemoteSkillPool:
        return self._cached_skill_pool or RemoteSkillPool([])

    # ── Control ──────────────────────────────────────────────────

    def cancel_run(self) -> None:
        asyncio.ensure_future(self._transport.send(msg.Cancel()))

    def toggle_verbose(self) -> bool:
        asyncio.ensure_future(self._rpc(RpcMethod.TOGGLE_VERBOSE))
        return True

    def wire_queue_hook(self, queue: list) -> None:
        # No-op in multi-process mode — queue lives on BE
        pass

    def wire_orchestrate_progress(self, callback: Callable) -> None:
        self._push_handlers["orchestrate_progress"] = lambda p: callback(p.get("line", ""))

    # ── Shutdown ─────────────────────────────────────────────────

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self._transport.send(msg.Shutdown())
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        await self._transport.close()
