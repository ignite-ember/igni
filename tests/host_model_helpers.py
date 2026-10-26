"""A running backend for the host object model's tests.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

:class:`HostRig` is the backend the way :class:`~ember_code.backend.app.BackendApp`
assembles it — a real :class:`SessionOrchestrator` over a real session
pool, real RPC tables built by the real :class:`RpcRouter`, the root
transport wrapped by the real :class:`ObservedTransport`, the host model
wired by the same :class:`HostModelWiring` the app uses, and the real
loopback :class:`HostChannel`. Only the sessions are stand-ins
(:class:`FakeSession`): a session is an agent and a model, and these
tests are about what reaches them, not what they answer.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ember_code.backend.command_result import CommandResult
from ember_code.backend.host_backend import HostModelWiring, ObservedTransport
from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import RpcRouter
from ember_code.backend.schemas_panels import OutputStyleInfo, OutputStylesResult
from ember_code.backend.schemas_rpc import GroupPolicyPackResult
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.backend.session_pool import SessionRuntime
from ember_code.backend.session_stamping_transport import SessionStampingTransport
from ember_code.core.config.schemas.display import DisplayConfig
from ember_code.core.session.event_log_schema import SessionEvent
from ember_code.extensions.host.channel import HostChannel
from ember_code.extensions.host.model import HostModel
from ember_code.extensions.host.wire import Op, Request
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import ExtensionStore
from ember_code.protocol import messages as msg

REPO = Path(__file__).resolve().parent.parent
SDK_SRC = REPO / "packages" / "igni-extension" / "src"


class CapturingTransport:
    """The windows, as a list: every message igni sent them."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []
        self.is_closed = False

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    def of(self, kind: type[msg.Message]) -> list[Any]:
        return [m for m in self.sent if isinstance(m, kind)]

    def pushes(self, channel: str) -> list[msg.PushNotification]:
        return [
            m for m in self.sent if isinstance(m, msg.PushNotification) and m.channel == channel
        ]


class FakeModels:
    def __init__(self) -> None:
        self.default = "small"
        self.max_context_window = 100_000
        self.registry: dict[str, Any] = {"small": {"model_id": "s"}, "large": {"model_id": "l"}}


class FakeSession:
    """One session's backend, standing in for :class:`BackendServer`.

    Implements the methods the RPC handlers and the message dispatcher
    call, answering with values of the shapes the real ones return, and
    records what it was asked.
    """

    def __init__(self, session_id: str, project_dir: Path) -> None:
        self.session_id = session_id
        self.project_dir = project_dir
        self.processing = False
        self.run_timeout = 600
        self.settings = SimpleNamespace(display=DisplayConfig(), models=FakeModels())
        self.runtime: Any = None
        self.ran: list[tuple[str, dict[str, str]]] = []
        self.commands: list[str] = []
        self.events: list[SessionEvent] = []
        self.answers: list[tuple[str, str, str]] = []
        self.switched_to: list[str] = []
        self.output_style = ""
        self.skill_names = ["review"]
        self.workflow_runner = None
        self.run_release: asyncio.Event | None = None

    # ── Lifecycle the pool and dispatcher touch ───────────────────

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    async def shutdown(self) -> None:
        return None

    def consume_plan_research_flag(self) -> bool:
        return False

    async def maybe_auto_name_session(self) -> Any:
        return SimpleNamespace(ok=False, name="")

    # ── Runs ──────────────────────────────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, str] | None = None
    ) -> AsyncIterator[msg.Message]:
        self.ran.append((text, dict(media or {})))
        run_id = f"run-{len(self.ran)}"
        self.processing = True
        yield msg.RunStarted(run_id=run_id, agent_name="main", model="small")
        yield msg.ContentDelta(text="On it.")
        if self.run_release is not None:
            await self.run_release.wait()
        self.processing = False
        yield msg.RunCompleted(run_id=run_id, input_tokens=3, output_tokens=2, duration=0.5)

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str
    ) -> AsyncIterator[msg.Message]:
        self.answers.append((requirement_id, action, choice))
        yield msg.Info(text=f"resolved {requirement_id}")

    async def resolve_hitl_batch(self, decisions: list[Any]) -> AsyncIterator[msg.Message]:
        for d in decisions:
            self.answers.append((d.requirement_id, d.action, d.choice))
        yield msg.Info(text="resolved")

    async def handle_command(self, text: str) -> msg.CommandResult:
        self.commands.append(text)
        if text.startswith("/output-style "):
            self.output_style = text.split(" ", 1)[1]
        return CommandResult.info(f"ran {text}")

    def cancel_run(self) -> None:
        return None

    # ── Reads ─────────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        return msg.StatusUpdate(
            model=self.settings.models.default,
            max_context=self.settings.models.max_context_window,
            cloud_connected=True,
            cloud_email="ada@acme.test",
            cloud_name="Ada",
            cloud_org="Acme",
        )

    async def list_sessions(self) -> msg.SessionListResult:
        return msg.SessionListResult(
            sessions=[msg.SessionListEntry(session_id=self.session_id, name="First", run_count=1)]
        )

    async def switch_session(self, session_id: str) -> msg.Info:
        self.switched_to.append(session_id)
        return msg.Info(text=f"Switched to session: {session_id}")

    async def get_chat_history(self, session_id: str) -> list[dict]:
        return [{"role": "user", "content": text} for text, _ in self.ran]

    async def search_chat(self, session_id: str, query: str, limit: int = 50) -> list[dict]:
        return [{"text": t} for t, _ in self.ran if query in t][:limit]

    async def get_pending_messages(self, session_id: str) -> list[dict]:
        return []

    async def get_interrupted_runs(self, session_id: str) -> list[dict]:
        return []

    async def count_context_tokens(self) -> int:
        return 1234

    async def context_breakdown(self) -> dict:
        return {"history": 1000, "system": 234}

    def get_latest_plan(self) -> dict:
        return {"latest": "", "history": [], "tasks": [], "state": ""}

    def get_todos(self) -> list[dict]:
        return [{"content": "write tests", "status": "pending"}]

    async def loop_status(self) -> dict:
        return {
            "active": False,
            "paused": False,
            "prompt": "",
            "iteration_index": 0,
            "iterations_remaining": 0,
            "cap_explicit": False,
        }

    def get_slash_commands(self) -> list[dict]:
        return [{"name": "/ctx", "description": "context"}]

    def get_skill_pool(self) -> Any:
        return SimpleNamespace(
            list_skills=lambda: [
                SimpleNamespace(name="review", description="Reviews", body="Review it.")
            ]
        )

    def get_skill_details(self) -> list[dict]:
        return [{"name": "review"}]

    def get_agent_details(self) -> list[dict]:
        return [{"name": "main"}]

    async def get_scheduled_tasks(self, include_done: bool = True) -> list[dict]:
        return [{"id": "t1", "description": "nightly"}]

    def list_background_processes(self) -> list[dict]:
        return []

    def get_mcp_servers(self) -> list[dict]:
        return [{"name": "github"}]

    def get_mcp_status(self) -> list[dict]:
        return [{"name": "github", "connected": False}]

    async def get_mcp_server_details(self) -> list[dict]:
        return [{"name": "github", "tools": []}]

    def get_plugin_details(self) -> list[dict]:
        return []

    def get_marketplaces(self) -> list[dict]:
        return []

    def get_hooks_details(self) -> list[dict]:
        return []

    def get_group_policy(self) -> GroupPolicyPackResult:
        return GroupPolicyPackResult()

    def get_output_styles(self) -> OutputStylesResult:
        return OutputStylesResult(
            active=self.output_style,
            styles=[OutputStyleInfo(name="terse", description="Short")],
        )

    def read_file(self, path: str) -> dict:
        return {
            "path": path,
            "contents": (self.project_dir / path).read_text(),
            "language": "",
            "error": "",
        }

    # ── Writes ────────────────────────────────────────────────────

    def toggle_verbose(self) -> bool:
        return self.settings.display.toggle_show_routing()

    def switch_model(self, model_name: str) -> msg.Info:
        self.settings.models.default = model_name
        return msg.Info(text=f"Switched to {model_name}")

    async def mcp_connect(self, server_name: str) -> msg.Info:
        return msg.Info(text=f"connected {server_name}")

    async def loop_pause(self) -> bool:
        return True

    def dispatch_visualization_action(self, action: str, params: dict | None = None) -> dict:
        return {"ok": True, "action": action, "params": dict(params or {})}

    async def truncate_history(self, session_id: str, run_id: str) -> dict:
        return {"ok": True, "run_id": run_id}

    # ── The event log ─────────────────────────────────────────────

    async def record_event(self, event_type: str, payload: dict, run_id: str = "") -> None:
        self.events.append(
            SessionEvent(seq=len(self.events) + 1, type=event_type, payload=payload, run_id=run_id)
        )

    def session_events(self, event_type: str) -> list[SessionEvent]:
        return [e for e in self.events if e.type == event_type]


class RigOrchestrator(SessionOrchestrator):
    """The real orchestrator, making stand-in sessions instead of real ones."""

    async def _create_runtime(self, session_id: str) -> SessionRuntime:
        backend = FakeSession(session_id, self._project_dir)
        stamped = SessionStampingTransport(self._transport, backend)
        bridge = self._push_bridge.for_transport(stamped, queue=[])
        router = RpcRouter(
            backend=backend,
            transport=stamped,
            login=self._login,
            push=bridge,
            extensions=self._extensions,
            origins=self._origins,
            notifications=self._notifications,
        )
        runtime = SessionRuntime(
            backend=backend, rpc_table=router.build_table(), queue=[], transport=stamped
        )
        backend.attach_runtime(runtime)
        return runtime


def manifest(extension_id: str = "acme.reviewer", **permissions: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "permissions": permissions,
            "contributes": {
                "pages": [{"kind": f"{extension_id}.home", "label": "Home"}],
                "settings": [{"key": "serviceUrl", "type": "string", "label": "Service"}],
            },
        }
    )


class HostRig:
    """A backend with the host model, as the app builds it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.project = tmp_path / "project"
        self.project.mkdir(parents=True, exist_ok=True)
        self.windows = CapturingTransport()
        self.transport = ObservedTransport(self.windows)
        self.registry = ExtensionRegistry()
        # The default store, as the RPC handlers use it; ``conftest``
        # points the home directory it lives under at a temp dir.
        self.store = ExtensionStore()
        self.session = FakeSession("boot", self.project)
        self.model: HostModel | None = None
        self.orchestrator: RigOrchestrator | None = None
        self.channel: HostChannel | None = None
        self._requests = 0

    async def start(self, *, strict: bool = True) -> HostRig:
        loop = asyncio.get_running_loop()
        queue: list[str] = []
        push = PushNotificationBridge(transport=self.transport, loop=loop, queue=queue)
        login = LoginCoordinator(backend=self.session, push_bridge=push)
        extensions = ExtensionRpc(self.registry)
        router = RpcRouter(
            backend=self.session,
            transport=self.transport,
            login=login,
            push=push,
            extensions=extensions,
        )
        settings = SimpleNamespace(
            storage=SimpleNamespace(data_dir=str(self.tmp / "data"), database_url=None)
        )
        self.orchestrator = RigOrchestrator(
            backend=self.session,
            transport=self.transport,
            settings=settings,
            project_dir=self.project,
            additional_dirs=None,
            rpc_router=router,
            rpc_table=router.build_table(),
            push_bridge=push,
            login=login,
            queue=queue,
            extensions=extensions,
        )
        self.orchestrator.setup_pool()
        self.model = HostModelWiring.build(
            orchestrator=self.orchestrator,
            transport=self.transport,
            registry=self.registry,
            project_dir=self.project,
            store=self.store,
            strict=strict,
        )
        self.channel = HostChannel(self.model)
        return self

    async def stop(self) -> None:
        if self.channel is not None:
            await self.channel.stop()
        if self.model is not None:
            await self.model.drain()

    def install(self, extension_id: str = "acme.reviewer", **permissions: Any) -> str:
        self.registry.install(manifest(extension_id, **permissions), state=ExtensionState.READY)
        return extension_id

    async def ask(
        self,
        extension_id: str,
        op: str,
        member: str,
        *,
        window: str | None = None,
        home_window: str | None = None,
        **params: Any,
    ) -> Any:
        """One request, as a channel would make it. Returns the reply.

        ``to=`` is the value of a ``set``; everything else is a parameter.
        """
        assert self.model is not None
        value = params.pop("to", None)
        request = Request(id=1, op=Op(op), path=member, window=window, params=params, value=value)
        return await self.model.handle(extension_id, request, home_window=home_window)

    async def window(
        self, window_id: str = "w-main", *, connection: str = "ws-1", session: str = ""
    ) -> str:
        """A window connects and hydrates its model, as ``clientState.ts`` does."""
        assert self.orchestrator is not None
        if session:
            self.orchestrator.client_state.set_value(window_id, "session-id", session)
        await self.window_model(window_id, "hydrate", connection_id=connection, kind="tauri")
        return window_id

    async def window_writes(
        self, window_id: str, field: str, value: Any, *, token: str = ""
    ) -> Any:
        """The window's own views write one field of its model."""
        return await self.window_model(window_id, "write", field=field, value=value, token=token)

    async def window_model(self, window_id: str, op: str, **args: Any) -> Any:
        """One ``window_model`` request from a window; its answer, or
        the error string when the model refused it."""
        assert self.orchestrator is not None
        self._requests += 1
        request_id = f"wm-{op}-{self._requests}"
        await self.orchestrator.dispatch(
            msg.RPCRequest(
                id=request_id, method="window_model", args={"op": op, "window": window_id, **args}
            )
        )
        [answer] = [m for m in self.windows.of(msg.RPCResponse) if m.id == request_id]
        return answer.error or answer.result

    async def window_pushes(self, window_id: str | None = None) -> list[dict[str, Any]]:
        """Every ``window_model_changed`` push so far, optionally for one
        window. Pushes are broadcast on the loop, so it settles first."""
        await self.settle()
        return [
            p.payload
            for p in self.windows.pushes("window_model_changed")
            if window_id is None or p.payload["window"] == window_id
        ]

    async def settle(self) -> None:
        """Let background dispatches and signal deliveries run."""
        for _ in range(20):
            await asyncio.sleep(0)
        assert self.orchestrator is not None and self.model is not None
        tasks = [t for t in self.orchestrator._in_flight if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        await self.model.drain()
        for _ in range(5):
            await asyncio.sleep(0)


class Sink:
    """A subscriber's signals, as a list."""

    def __init__(self) -> None:
        self.got: list[tuple[str, Any]] = []

    def deliver(self, message: Any) -> None:
        self.got.append((message.signal, message.value))

    def values(self, wire: str) -> list[Any]:
        return [value for signal, value in self.got if signal == wire]


def extension_process_env(env: dict[str, str]) -> dict[str, str]:
    """What igni starts a process with, plus the SDK on its path."""
    merged = dict(os.environ)
    merged.update(env)
    merged["PYTHONPATH"] = os.pathsep.join([str(SDK_SRC), merged.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    return merged


PYTHON = sys.executable
