"""Per-message dispatcher.

Replaces the 11-branch ``isinstance`` chain that lived as
``_handle_message`` in :mod:`ember_code.backend.__main__`. Each
message class now maps to a bound method on :class:`MessageDispatcher`;
:meth:`dispatch` is the single entry point the receive loop calls.

The dispatcher's collaborators are:

* the target ``backend`` (per-runtime — the pool picks it before
  dispatch),
* the target ``transport`` (a stamping wrapper for pooled runtimes,
  the raw transport for the default runtime),
* the ``rpc_table`` from :class:`RpcRouter.build_table`,
* the per-runtime queue for injected user messages,
* an optional :class:`LoginCoordinator` (only the default-runtime
  dispatcher owns login; pooled runtimes get ``None``).

The old ``login_state: dict[str, Any] | None`` bag is gone —
callers now pass a real :class:`LoginCoordinator` or ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.protocol import messages as msg
from ember_code.protocol.messages import Message

logger = logging.getLogger(__name__)


class MessageDispatcher:
    """Route one wire message to its per-message-class handler.

    Instances are cheap — the pool creates one per runtime so each
    dispatcher can hold direct references to its runtime's
    backend / transport / queue rather than passing them through
    every call site.
    """

    def __init__(
        self,
        *,
        backend: Any,
        transport: Any,
        rpc_table: dict,
        queue: list[str],
        login: LoginCoordinator | None,
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._rpc_table = rpc_table
        self._queue = queue
        self._login = login

    async def dispatch(self, message: Any) -> None:
        req_id = message.id or ""
        match message:
            case msg.UserMessage():
                await self._on_user_message(message, req_id)
            case msg.HITLResponse():
                await self._on_hitl_response(message, req_id)
            case msg.HITLResponseBatch():
                await self._on_hitl_response_batch(message, req_id)
            case msg.Command():
                await self._on_command(message, req_id)
            case msg.SessionList():
                await self._on_session_list(req_id)
            case msg.SessionSwitch():
                await self._on_session_switch(message, req_id)
            case msg.ModelSwitch():
                await self._on_model_switch(message, req_id)
            case msg.MCPToggle():
                await self._on_mcp_toggle(message, req_id)
            case msg.QueueMessage():
                await self._on_queue_message(message)
            case msg.Typing():
                await self._on_typing(message)
            case msg.Cancel():
                self._on_cancel()
            case msg.CancelLogin():
                await self._on_cancel_login()
            case msg.RPCRequest():
                await self._on_rpc_request(message, req_id)
            case _:
                logger.warning("Unknown FE message type: %s", type(message).__name__)

    # ── User-driven message paths ────────────────────────────────

    async def _on_user_message(self, message: Any, req_id: str) -> None:
        # Mirroring: every attached view paints the user bubble; the
        # sender recognises its own client_id and skips the echo.
        # Send the ORIGINAL (un-wrapped) text so chat bubbles stay
        # clean — the system-context wrapper below is for the LLM
        # only, not for display.
        await self._transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id)
        )
        agent_text = self._maybe_wrap_plan_hint(message.text)
        async for proto in self._backend.run_message(agent_text, media=message.file_contents):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await self._transport.send(proto)
        await self._transport.send(msg.StreamEnd(id=req_id))

        # First completed run: name the session in the background so
        # a queued follow-up isn't blocked on the naming model call.
        self._spawn_auto_name()

    def _maybe_wrap_plan_hint(self, text: str) -> str:
        """One-shot ``/plan`` → ``enter_plan_mode`` nudge.

        When the user just typed ``/plan`` (slash command armed
        ``_plan_research_armed`` on the session), prepend a
        ``<system-context>`` instruction so the agent spawns the
        ``plan_researcher`` sub-agent on this exact request.

        Cleared after one use — subsequent turns in the same plan
        mode session don't get the hint again. FE strips
        ``<system-context>`` blocks on display, so the chat bubble
        shows only what the user typed.
        """
        armed = self._backend.consume_plan_research_flag()
        if not armed:
            return text
        return (
            "<system-context>\n"
            "Plan mode was just entered via the user's /plan slash "
            "command. BEFORE doing any other work, call "
            "`enter_plan_mode(task=...)` with the user's request "
            "below as the ``task`` argument so the plan_researcher "
            "sub-agent runs first and produces a structured "
            "Findings + Proposed Plan + Open Questions report. "
            "Do not skip this step — the researcher's report is "
            "what makes the eventual ``exit_plan_mode`` call "
            "grounded enough to pass the confidence validator.\n"
            "</system-context>\n\n" + text
        )

    def _spawn_auto_name(self) -> None:
        """Fire-and-forget: name the session after the first
        completed run. The task handle is parked on the
        :class:`SessionRuntime` so the loop doesn't GC it mid-flight.

        Falls back to an orphaned task when no runtime is attached
        (test harnesses that construct dispatchers without booting
        an orchestrator) — the coroutine finishes quickly and the
        transport.send is idempotent-safe.
        """
        coro = self._auto_name_coro()
        runtime = self._backend.runtime
        if runtime is not None:
            runtime.spawn_auto_name(coro)
        else:
            asyncio.create_task(coro)

    async def _auto_name_coro(self) -> None:
        # ``maybe_auto_name_session`` now returns an ``AutoNameResult``
        # envelope; the FE push fires only when a fresh name was
        # generated (``ok=True``), matching the pre-refactor
        # truthy-string check byte-for-byte.
        result = await self._backend.maybe_auto_name_session()
        if not result.ok or not result.name:
            return
        await self._transport.send(
            msg.push_session_named(
                {"session_id": self._backend.session_id, "name": result.name},
            )
        )

    async def _on_hitl_response(self, message: Any, req_id: str) -> None:
        # Mirroring: dismiss the now-stale permission dialog on
        # every other view before the resumed run starts streaming.
        await self._transport.send(msg.RequirementResolved(requirement_id=message.requirement_id))
        async for proto in self._backend.resolve_hitl(
            message.requirement_id, message.action, message.choice
        ):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await self._transport.send(proto)
        await self._transport.send(msg.StreamEnd(id=req_id))

    async def _on_hitl_response_batch(self, message: Any, req_id: str) -> None:
        for decision in message.decisions:
            await self._transport.send(
                msg.RequirementResolved(requirement_id=decision.requirement_id)
            )
        async for proto in self._backend.resolve_hitl_batch(message.decisions):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await self._transport.send(proto)
        await self._transport.send(msg.StreamEnd(id=req_id))

    async def _on_command(self, message: Any, req_id: str) -> None:
        result = await self._backend.handle_command(message.text)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_session_list(self, req_id: str) -> None:
        result = await self._backend.list_sessions()
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_session_switch(self, message: Any, req_id: str) -> None:
        result = await self._backend.switch_session(message.session_id)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_model_switch(self, message: Any, req_id: str) -> None:
        result = self._backend.switch_model(message.model_name)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_mcp_toggle(self, message: Any, req_id: str) -> None:
        result = await self._backend.toggle_mcp(message.server_name, message.connect)
        result = result.model_copy(update={"id": req_id})
        await self._transport.send(result)

    async def _on_queue_message(self, message: Any) -> None:
        self._queue.append(message.text)
        await self._transport.send(
            msg.UserMessageReceived(text=message.text, client_id=message.client_id, queued=True)
        )

    async def _on_typing(self, message: Any) -> None:
        """Pure fan-out: the BE adds nothing. Relayed to ALL views
        (sender included — it filters by ``client_id``)."""
        await self._transport.send(message)

    def _on_cancel(self) -> None:
        self._backend.cancel_run()

    async def _on_cancel_login(self) -> None:
        if self._login is not None:
            await self._login.cancel()

    async def _on_rpc_request(self, message: Any, req_id: str) -> None:
        handler = self._rpc_table.get(message.method)
        if handler is None:
            await self._transport.send(
                msg.RPCResponse.fail(req_id, f"Unknown RPC method: {message.method}")
            )
            return
        try:
            result = handler(message.args)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            if isinstance(result, Message):
                result = result.model_copy(update={"id": req_id})
                await self._transport.send(result)
            else:
                await self._transport.send(msg.RPCResponse.ok(req_id, _serialize(result)))
        except Exception as exc:
            logger.error("RPC %s failed: %s", message.method, exc, exc_info=True)
            await self._transport.send(msg.RPCResponse.fail(req_id, str(exc)))


def _serialize(value: Any) -> Any:
    """Convert an RPC handler's return value into a JSON-safe
    shape for :class:`RPCResponse.result`.

    Order of branches matters:

    1. ``None`` → ``None`` (distinct from empty string / empty dict).
    2. Scalars (``str``, ``int``, ``float``, ``bool``) — passed as-is.
       ``bool`` MUST branch here before any ``isinstance(_, int)``
       fallback because ``isinstance(True, int)`` is ``True`` in
       Python and we don't want ``True`` coerced to ``1``.
    3. Sequences / mappings — recurse element-wise.
    4. Pydantic models — ``model_dump``.
    5. Anything else — ``str()`` as a last resort so an exotic type
       doesn't crash the JSON encoder downstream.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)
