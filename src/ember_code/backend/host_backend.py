"""igni's backend, as the host object model reaches it.

Two pieces, both thin:

* :class:`ObservedTransport` wraps the backend's root transport, once.
  Everything igni sends any window passes through it, so the host model
  sees every broadcast on its way out and turns it into signals; and an
  action the host sends on an extension's behalf whose answer belongs to
  the extension (a slash command it ran) is taken back here instead of
  being shown in the windows.
* :class:`OrchestratorHostBackend` implements
  :class:`~ember_code.extensions.host.backend.HostBackend` over the
  session pool: an RPC is the handler the window would have reached, an
  action is dispatched as the window's would be, an attach is the
  window's attach — refused where the window's is.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ember_code.backend.activity import Activity
from ember_code.backend.message_dispatcher import _serialize
from ember_code.backend.rpc_router import POOL_LEVEL_RPCS
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.core.approvals.events import approval_events
from ember_code.core.hooks.extension_gate import ExtensionHookGate
from ember_code.extensions.host.broadcasts import BroadcastSignals
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.model import HostModel
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.extensions.store import ExtensionStore
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)

#: Ids the host gives actions whose answer it takes back.
HOST_ID_PREFIX = "host-"


class ObservedTransport:
    """The root transport, with observers on the way out."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._observers: list[Callable[[msg.Message], None]] = []
        self._waiting: dict[str, asyncio.Future[msg.Message]] = {}

    @property
    def inner(self) -> Any:
        return self._inner

    def observe(self, observer: Callable[[msg.Message], None]) -> None:
        self._observers.append(observer)

    def expect(self, message_id: str) -> asyncio.Future[msg.Message]:
        """The next message sent with ``message_id`` comes here, not to the windows."""
        future: asyncio.Future[msg.Message] = asyncio.get_running_loop().create_future()
        self._waiting[message_id] = future
        return future

    def forget(self, message_id: str) -> None:
        self._waiting.pop(message_id, None)

    async def send(self, message: msg.Message) -> None:
        if message.id and message.id in self._waiting:
            future = self._waiting.pop(message.id)
            if not future.done():
                future.set_result(message)
            return
        for observer in list(self._observers):
            try:
                observer(message)
            except Exception:
                logger.exception("an outbound observer failed on %s", message.type)
        await self._inner.send(message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RpcWatch:
    """Which window RPCs are in flight, so a success can be signalled.

    "Every write is a signal": a window's write through an RPC broadcasts
    nothing of its own, so the host learns it succeeded from the reply
    going out and tells
    :meth:`~ember_code.extensions.host.broadcasts.BroadcastSignals.after_rpc`.
    """

    LIMIT = 1000

    def __init__(self, after: Callable[[RpcMethod, dict[str, Any], str, Any], None]) -> None:
        self._after = after
        self._pending: OrderedDict[str, tuple[RpcMethod, dict[str, Any], str]] = OrderedDict()

    def inbound(self, message: Any) -> None:
        if not isinstance(message, msg.RPCRequest) or not message.id:
            return
        try:
            method = RpcMethod(message.method)
        except ValueError:
            return
        if method in POOL_LEVEL_RPCS:
            return
        self._pending[message.id] = (method, dict(message.args or {}), message.session_id)
        while len(self._pending) > self.LIMIT:
            self._pending.popitem(last=False)

    def outbound(self, message: msg.Message) -> None:
        if not message.id or message.id not in self._pending:
            return
        method, args, session_id = self._pending.pop(message.id)
        if isinstance(message, msg.RPCResponse):
            if message.error:
                return
            result = message.result
        else:
            if message.type == "error":
                return
            result = message.model_dump()
        self._after(method, args, session_id or message.session_id, result)


class OrchestratorHostBackend:
    """:class:`~ember_code.extensions.host.backend.HostBackend` over the pool."""

    def __init__(
        self,
        *,
        orchestrator: SessionOrchestrator,
        transport: ObservedTransport,
        project_dir: Path,
        client_state: Any,
    ) -> None:
        self._orchestrator = orchestrator
        self._transport = transport
        self.project_dir = project_dir
        self.client_state = client_state
        self._tasks: set[asyncio.Task[None]] = set()

    def default_session(self) -> str:
        return str(self._orchestrator.pool.default.backend.session_id)

    def live_sessions(self) -> list[str]:
        return [str(rt.backend.session_id) for rt in self._orchestrator.pool.runtimes]

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        if method in POOL_LEVEL_RPCS:
            raise ValueError(f"{method.value} is answered by the host model, not called through it")
        runtime = await self._orchestrator.pool.get_or_create(session_id)
        handler = runtime.rpc_table[method]
        result = handler(dict(args))
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            result = await result
        return _serialize(result)

    async def act(self, message: msg.Message, *, session_id: str) -> None:
        self._orchestrator.spawn_dispatch(message.model_copy(update={"session_id": session_id}))

    async def ask(
        self, message: msg.Message, *, session_id: str, timeout: float = 120.0
    ) -> msg.Message:
        message_id = f"{HOST_ID_PREFIX}{uuid.uuid4().hex}"
        answer = self._transport.expect(message_id)
        self._orchestrator.spawn_dispatch(
            message.model_copy(update={"id": message_id, "session_id": session_id})
        )
        try:
            return await asyncio.wait_for(answer, timeout)
        finally:
            self._transport.forget(message_id)

    async def attach(self, session_id: str, project_dir: str | None) -> dict[str, str]:
        result = await self._orchestrator.attach(session_id, project_dir)
        return {"session_id": result.session_id, "project_dir": result.project_dir}

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        queued: bool,
    ) -> None:
        await self._orchestrator.post_from_extension(
            session_id=session_id,
            extension_id=extension_id,
            text=text,
            media=media,
            queued=queued,
        )

    async def show_view(self, *, session_id: str, payload: dict[str, Any], run_id: str) -> None:
        runtime = await self._orchestrator.pool.get_or_create(session_id)
        await runtime.transport.send(
            msg.PushNotification(channel="orchestrate_event", payload=payload)
        )
        record = getattr(runtime.backend, "record_event", None)
        if record is not None:
            await record("visualization_delta", payload, run_id)

    def views(self, session_id: str) -> list[dict[str, Any]]:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is None:
            return []
        events = getattr(runtime.backend, "session_events", None)
        if events is None:
            return []
        latest: dict[str, dict[str, Any]] = {}
        for event in events("visualization_delta"):
            payload = dict(event.payload or {})
            if not payload.get("final", True) or not payload.get("spec_id"):
                continue
            latest[str(payload["spec_id"])] = BroadcastSignals.view(session_id, payload)
        return list(latest.values())

    def activity(self) -> dict[str, Any]:
        return self.activity_model().model_dump(mode="json")

    def activity_model(self) -> Activity:
        sessions = []
        for runtime in self._orchestrator.pool.runtimes:
            probe = getattr(runtime.backend, "activity", None)
            if probe is not None:
                sessions.append(probe())
        return Activity.of(sessions)

    def waiting_approvals(self, session_id: str) -> list[msg.Message]:
        runtime = self._orchestrator.pool.find(session_id)
        hitl = getattr(runtime.backend, "hitl", None) if runtime is not None else None
        if hitl is None:
            return []
        return list(hitl.waiting_messages())

    def project_dir_of(self, session_id: str) -> Path:
        runtime = self._orchestrator.pool.find(session_id)
        if runtime is None:
            return self.project_dir
        return Path(runtime.backend.project_dir)

    def broadcast(self, message: msg.Message) -> None:
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._transport.send(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


class HostModelWiring:
    """The host model over a session pool, told everything it listens to.

    One place for the wiring so the backend and its tests build the
    same thing: four taps — every outbound message, every window RPC
    (to signal writes), every registry change, and the workspace poll —
    and the window connections closing.
    """

    @staticmethod
    def build(
        *,
        orchestrator: SessionOrchestrator,
        transport: ObservedTransport,
        registry: ExtensionRegistry,
        project_dir: Path,
        store: ExtensionStore | None = None,
        notifications: Any = None,
        notices: Any = None,
        workspace_signals: Any = None,
        strict: bool = False,
    ) -> HostModel:
        backend = OrchestratorHostBackend(
            orchestrator=orchestrator,
            transport=transport,
            project_dir=project_dir,
            client_state=orchestrator.client_state,
        )
        model = HostModel(
            backend=backend,
            registry=registry,
            store=store,
            notifications=notifications,
            notices=notices,
            strict=strict,
        )
        orchestrator.attach_host_model(model)
        # igni's hook events reach the extensions that hook them.
        ExtensionHookGate.install(ExtensionHooks.wire(registry))
        transport.observe(model.outbound)
        watch = RpcWatch(model.broadcasts.after_rpc)
        orchestrator.watch_inbound(watch.inbound)
        transport.observe(watch.outbound)
        registry.watch(model.extension_changed)
        # An app following approvals through the API is told of every
        # one that appears or goes away, wherever it was answered.
        approval_events.watch(model.broadcasts.approval_changed)
        if workspace_signals is not None:
            workspace_signals.attach_model(model)
        watch_connections = getattr(transport.inner, "watch_connections", None)
        if watch_connections is not None:

            def closed(event: str, connection_id: str) -> None:
                if event == "detached":
                    model.connection_closed(connection_id)

            watch_connections(closed)
        return model


__all__ = [
    "HOST_ID_PREFIX",
    "HostModelWiring",
    "ObservedTransport",
    "OrchestratorHostBackend",
    "RpcWatch",
]
