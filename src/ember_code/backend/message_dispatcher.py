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
from ember_code.core.gates import GateVerdict
from ember_code.extensions.host.intercepts import Interceptors
from ember_code.extensions.registry import ExtensionRefused
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.protocol import messages as msg
from ember_code.protocol.messages import Message

logger = logging.getLogger(__name__)


class ExtensionMessage:
    """How a message an extension posts reads to the agent.

    The agent must never take it for the person's: it says who sent it,
    in a block the windows strip from display (``<system-context>``),
    around the text wrapped in a tag the web client reads back on
    reload to label the bubble (``chat/model.ts``).
    """

    TAG = "extension-message"

    @classmethod
    def wrap(cls, extension_id: str, text: str) -> str:
        return (
            "<system-context>\n"
            f"The message below was posted by the igni extension '{extension_id}', "
            "not typed by the person. Answer it in the chat as you would answer them.\n"
            "</system-context>\n"
            f'<{cls.TAG} from="{extension_id}">\n{text}\n</{cls.TAG}>'
        )


class ChatSubmissions:
    """``chat.submitting``: a message put to the extensions that intercept it.

    Asked where a message enters — the person's, an extension's post, a
    queued one — before igni wraps it for the agent, so an interceptor
    reads what was written, and may change it.
    """

    EVENT = "chat.submitting"

    def __init__(self, interceptors: Interceptors | None = None) -> None:
        self._interceptors = interceptors if interceptors is not None else Interceptors.shared()

    async def check(
        self,
        session_id: str,
        text: str,
        *,
        from_extension: str | None = None,
        has_media: bool = False,
    ) -> GateVerdict:
        """The verdict; ``verdict.value["text"]`` is the text to go ahead with."""
        value = {
            "session_id": session_id,
            "text": text,
            "from_extension": from_extension,
            "has_media": has_media,
        }
        if not self._interceptors.any_for(self.EVENT):
            return GateVerdict(allowed=True, value=value)
        return await self._interceptors.run(self.EVENT, value, writer=from_extension)


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
        extensions: ExtensionRpc | None = None,
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._rpc_table = rpc_table
        self._queue = queue
        self._login = login
        # Optional so every existing construction site keeps working.
        # Absent, an ``ext.*`` call is refused with the reason rather
        # than falling through to ``Unknown RPC method``, which is
        # what it did for as long as the namespace did not exist and
        # is the least informative thing it could say.
        self._extensions = extensions
        self._submissions = ChatSubmissions()

    async def _submitted(
        self,
        text: str,
        req_id: str,
        *,
        from_extension: str | None = None,
        has_media: bool = False,
    ) -> str | None:
        """The text to run, or ``None`` when an extension stopped it (and the views were told)."""
        verdict = await self._submissions.check(
            str(self._backend.session_id),
            text,
            from_extension=from_extension,
            has_media=has_media,
        )
        if verdict.allowed:
            return str(verdict.value.get("text", text))
        await self._transport.send(msg.Error(id=req_id, text=verdict.reason))
        return None

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
        text = await self._submitted(message.text, req_id, has_media=bool(message.file_contents))
        if text is None:
            return
        # Mirroring: every attached view paints the user bubble; the
        # sender recognises its own client_id and skips the echo.
        # Send the ORIGINAL (un-wrapped) text so chat bubbles stay
        # clean — the system-context wrapper below is for the LLM
        # only, not for display. An interceptor's change is what was
        # sent, so it is what the bubble shows.
        await self._transport.send(msg.UserMessageReceived(text=text, client_id=message.client_id))
        # ``force=true`` is the FE's "Retry" semantic on an
        # interrupted banner: supersede any pending + interrupted
        # rows for this session, then start clean. Without this
        # the retry would create a second pending row that dangles
        # until the new run completes (and on failure, would leave
        # BOTH the old and new rows stamped interrupted). Done
        # before ``run_message`` so the inner ``record_received``
        # call lands on a clean slate.
        if message.force:
            journal = getattr(self._backend, "_runs", None)
            if journal is not None:
                discarded = await journal.pending_journal.discard_all_for_session()
                logger.debug(
                    "user_message(force=true) discarded %d stale row(s) for session",
                    discarded,
                )
        agent_text = self._maybe_wrap_plan_hint(text)
        await self._run_turn(agent_text, message.file_contents, req_id)

    async def _run_turn(self, agent_text: str, media: dict[str, str], req_id: str) -> None:
        """Run one turn and stream it to every view."""
        async for proto in self._backend.run_message(agent_text, media=media):
            if req_id:
                proto = proto.model_copy(update={"id": req_id})
            await self._transport.send(proto)
        await self._transport.send(msg.StreamEnd(id=req_id))

        # First completed run: name the session in the background so
        # a queued follow-up isn't blocked on the naming model call.
        self._spawn_auto_name()

    # ── An extension's messages ──────────────────────────────────

    async def post_from_extension(
        self, extension_id: str, text: str, media: dict[str, str]
    ) -> None:
        """``host.chat.post``: a message from an extension, for the agent.

        Every view paints it labelled as the extension's — never as the
        person's, which an extension cannot post as — and the agent is
        told who sent it, then answers in the chat as it answers the
        person.
        """
        submitted = await self._submitted(
            text, "", from_extension=extension_id, has_media=bool(media)
        )
        if submitted is None:
            return
        await self._transport.send(
            msg.UserMessageReceived(text=submitted, client_id="", extension_id=extension_id)
        )
        await self._run_turn(ExtensionMessage.wrap(extension_id, submitted), media, "")

    async def queue_from_extension(self, extension_id: str, text: str) -> None:
        """``host.chat.queue``: the same, for the next turn."""
        submitted = await self._submitted(text, "", from_extension=extension_id)
        if submitted is None:
            return
        text = submitted
        self._queue.append(ExtensionMessage.wrap(extension_id, text))
        await self._transport.send(
            msg.UserMessageReceived(text=text, client_id="", queued=True, extension_id=extension_id)
        )

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
        text = await self._submitted(message.text, message.id or "")
        if text is None:
            return
        self._queue.append(text)
        await self._transport.send(
            msg.UserMessageReceived(text=text, client_id=message.client_id, queued=True)
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
        # The extension namespace is checked before the base table,
        # not after. A base method could otherwise shadow an
        # extension's by being named ``ext.something`` — unlikely, but
        # the failure would be an extension silently served by igni's
        # own handler, which is the worst shape a routing bug can
        # take.
        if ExtensionRpc.owns(message.method):
            await self._on_extension_rpc(message, req_id)
            return

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
        except KeyError as exc:
            # A handler reading `args["session_id"]` on a call that
            # omitted it raises `KeyError('session_id')`, and
            # `str(exc)` is the bare `"'session_id'"`. That reached the
            # client as the entire error message: no method, no
            # indication it was about an argument at all. Sweeping the
            # API, `check_permission` gave up its three required keys
            # one refusal at a time — `tool_args`, then `tool_name`,
            # then `func_name` — with nothing to say they were
            # arguments rather than a fault.
            #
            # Two handlers already did this properly
            # (`run_workflow: 'name' is required`), so the wording is
            # theirs; this makes it the default instead of the
            # exception. Narrowed to `KeyError` on purpose: a
            # `KeyError` raised *inside* real logic is now slightly
            # mislabelled, which is the cost of not having to annotate
            # every handler, and it is a better trade than the caller
            # being told only `'url'`.
            logger.error("RPC %s failed: missing argument %s", message.method, exc)
            await self._transport.send(
                msg.RPCResponse.fail(req_id, f"{message.method}: {exc} is a required argument")
            )
        except Exception as exc:
            logger.error("RPC %s failed: %s", message.method, exc, exc_info=True)
            await self._transport.send(msg.RPCResponse.fail(req_id, str(exc)))

    async def _on_extension_rpc(self, message: Any, req_id: str) -> None:
        """Serve one ``ext.<id>.<method>`` call.

        Separate from :meth:`_on_rpc_request` because the failure
        modes have nothing in common. A base RPC fails by raising out
        of a handler; an extension call fails by being refused, and a
        refusal is a sentence written for the extension's author with
        the manifest line that would fix it. Folding the two together
        would mean wrapping refusals in the "is a required argument"
        machinery above, which is about something else entirely.
        """
        if self._extensions is None:
            await self._transport.send(
                msg.RPCResponse.fail(
                    req_id,
                    f"{message.method}: this backend has no extension support, "
                    "so nothing can answer the 'ext.' namespace.",
                )
            )
            return
        try:
            result = await self._extensions.dispatch(
                caller=getattr(message, "caller", None),
                method=message.method,
                args=message.args,
            )
        except ExtensionRefused as exc:
            # Expected, and not worth a traceback: a refusal is the
            # system working. Logged at info so an administrator
            # chasing "why can it not call this" has a record.
            logger.info("extension rpc %s refused: %s", message.method, exc)
            await self._transport.send(msg.RPCResponse.fail(req_id, str(exc)))
            return
        except Exception as exc:
            logger.error("extension rpc %s failed: %s", message.method, exc, exc_info=True)
            await self._transport.send(msg.RPCResponse.fail(req_id, str(exc)))
            return
        await self._transport.send(msg.RPCResponse.ok(req_id, _serialize(result)))


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
