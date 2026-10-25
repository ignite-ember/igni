"""Composition root for the backend process.

Replaces the ~700-line ``_run`` function that used to live in
:mod:`ember_code.backend.__main__`. That function was one big
imperative script — every collaborator was a nested closure, every
piece of shared state was a captured local, and shutdown ordering
lived in a wall of ``with contextlib.suppress`` blocks. This module
turns the same lifecycle into two classes:

* :class:`TransportBuilder` — pick the transport shape (Unix, WS,
  or the composite of both) from the CLI flags.
* :class:`BackendApp` — the composition root. Its constructor
  wires every coordinator together; its :meth:`run` method walks
  the lifecycle in order and hands ``teardown`` to
  :class:`BackendSupervisor` in a ``finally`` block.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ember_code.backend.agent_refresh import AgentRefresh
from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.host_backend import HostModelWiring, ObservedTransport
from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import RpcRouter
from ember_code.backend.schemas_rpc import BackendReadyLine
from ember_code.backend.server import BackendServer
from ember_code.backend.session_orchestrator import SessionOrchestrator
from ember_code.backend.supervisor import BackendSupervisor
from ember_code.backend.workspace_signals import WorkspaceSignals
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.plugins.installer import PluginInstaller
from ember_code.core.prompts.contributions import prompt_contributions
from ember_code.extensions.contributions import (
    ExtensionConditionReceiver,
    ExtensionContributions,
)
from ember_code.extensions.distribution import ExtensionDistribution
from ember_code.extensions.health import ExtensionHealth
from ember_code.extensions.host.channel import HostChannel
from ember_code.extensions.host.model import HostModel
from ember_code.extensions.impact import IgniCommands
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.launcher import ExtensionLauncher
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.profile import DeploymentProfile, DevelopmentFlag
from ember_code.extensions.python_env import PythonEnvironments
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import (
    CredentialsSession,
    PageServiceTokens,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
)
from ember_code.extensions.store import ExtensionStore
from ember_code.notifications.center import NotificationCenter
from ember_code.notifications.extension_notices import ExtensionNoticeReceiver
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.server_client import SignedInServer

logger = logging.getLogger(__name__)


class TransportBuilder:
    """Pick and build the right transport for the CLI flags.

    Both flags together = mirrored session: the TUI attaches over
    the Unix socket while GUI tabs attach over WS, and every view
    receives every broadcast (via the composite transport's fan-out).
    """

    def __init__(self, *, socket_path: str | None, ws_port: int | None) -> None:
        self._socket_path = socket_path
        self._ws_port = ws_port
        self._ws_transport: Any = None

    @property
    def ws_transport(self) -> Any:
        """The WS transport instance (if any). Exposed so the app
        can read the bound port for the ready line + lockfile."""
        return self._ws_transport

    async def build(self) -> Any:
        children: list[Any] = []
        if self._socket_path is not None:
            from ember_code.transport.unix_socket import UnixSocketServerTransport

            children.append(UnixSocketServerTransport(self._socket_path))
        if self._ws_port is not None:
            from ember_code.transport.websocket import WebSocketServerTransport

            self._ws_transport = WebSocketServerTransport(port=self._ws_port)
            children.append(self._ws_transport)

        if len(children) == 1:
            transport: Any = children[0]
        else:
            from ember_code.transport.websocket import CompositeTransport

            transport = CompositeTransport(children)
        await transport.start()
        return transport


class BackendApp:
    """The BE's composition root.

    Owns the whole lifecycle end-to-end: transport build → backend
    construct → collaborator wire-up → ready line → serve →
    shutdown. Every collaborator is stored as an instance attribute
    so tests can construct the app with dependency-injected
    replacements if needed.
    """

    def __init__(
        self,
        *,
        socket_path: str | None,
        ws_port: int | None,
        project_dir: Path,
        resume_session_id: str | None,
        additional_dirs: list[Path] | None,
    ) -> None:
        self._socket_path = socket_path
        self._ws_port = ws_port
        self._project_dir = project_dir
        self._resume_session_id = resume_session_id
        self._additional_dirs = additional_dirs

        self._transport_builder = TransportBuilder(socket_path=socket_path, ws_port=ws_port)
        # Wired in ``run`` — kept as attributes so shutdown/teardown
        # can access whatever the boot happened to build.
        self._transport: Any = None
        self._backend: BackendServer | None = None
        self._supervisor: BackendSupervisor | None = None
        self._push_bridge: PushNotificationBridge | None = None
        self._login: LoginCoordinator | None = None
        self._rpc_router: RpcRouter | None = None
        self._orchestrator: SessionOrchestrator | None = None
        self._queue: list[str] = []
        # Built in ``_start_extensions``, once there is an MCP manager
        # to start extensions with.
        self._distribution: ExtensionDistribution | None = None
        self._service_tokens: ServiceTokenEndpoint | None = None
        self._notifications: NotificationCenter | None = None
        self._notices: ExtensionNoticeReceiver | None = None
        # The host object model and the socket processes reach it on.
        self._host_model: HostModel | None = None
        self._host_channel: HostChannel | None = None

    def _load_settings_with_group_policy(self) -> Settings:
        """Build this process's ``Settings`` with the org's policy applied.

        This is the backend's half of SWP-23. ``GroupPolicyTier`` has always
        known how to merge a group's ``settings`` entries between the CLI and
        the sysadmin file, and nothing ever handed it a pack — so an admin
        publishing ``{"permissions": {"web_fetch": "deny"}}`` was ignored, silently,
        everywhere. Here is where that is turned on: the backend is the process
        that owns the session, holds the cloud token, and starts exactly once,
        which is all three things the tier needs.

        Loaded twice, on purpose, and the second load is the one that counts.
        The cache the policy lives in sits under ``storage.data_dir``, and
        ``storage.data_dir`` is itself a setting — so the first pass exists
        only to answer "which directory", using local configuration. A group
        that relocates ``data_dir`` therefore does not move its own cache,
        which is the only sane reading: the machine decides where igni keeps
        its files, the org decides what is in them. Both passes are file reads
        of a handful of small YAML/JSON files; neither touches the network.

        The fetcher reads the pack ``AuthController`` last wrote to disk — the
        live portal call cannot happen here, because the token this process
        authenticates with is read out of the ``Settings`` being built. See
        :class:`~ember_code.core.config.group_policy.CachedSettingsFetcher`.
        """
        from ember_code.core.config.group_policy import CachedSettingsFetcher

        local_only = load_settings(project_dir=self._project_dir)
        return load_settings(
            project_dir=self._project_dir,
            group_policy_fetcher=CachedSettingsFetcher(local_only.storage.data_dir),
        )

    async def run(self) -> None:
        """Boot, serve, and tear down. Exceptions in the boot or
        serve phases are logged; teardown always runs."""
        # Wrapped once, at the root, so the host object model sees
        # every message igni sends any window — see ``host_backend``.
        self._transport = ObservedTransport(await self._transport_builder.build())

        settings = self._load_settings_with_group_policy()
        self._backend = BackendServer(
            settings,
            project_dir=self._project_dir,
            resume_session_id=self._resume_session_id,
            additional_dirs=self._additional_dirs,
        )
        # Async post-construction wiring (hydrate any persisted
        # ``/loop`` state from the project's ``state.db``).
        await self._backend.startup()

        loop = asyncio.get_running_loop()
        self._supervisor = BackendSupervisor(
            transport=self._transport,
            loop=loop,
            project_dir=self._project_dir,
        )
        self._supervisor.set_backend_fallback(self._backend)
        self._supervisor.install_signal_handlers()
        self._supervisor.start_parent_watchdog()
        self._supervisor.start_transport_close_watcher()

        # Push bridge — the sink every callback/listener ends up
        # pushing through.
        self._push_bridge = PushNotificationBridge(
            transport=self._transport,
            loop=loop,
            queue=self._queue,
        )
        self._backend.wire_queue_hook(self._queue)
        self._push_bridge.wire_all(self._backend)

        self._login = LoginCoordinator(backend=self._backend, push_bridge=self._push_bridge)
        # Process-level, not per-session. An extension is installed
        # into a deployment, so a second session must not see a
        # different set of them — and the boot RPC table is built
        # here, before the orchestrator exists, so this is the level
        # that can hand the same objects to both.
        self._extensions = ExtensionRpc(ExtensionRegistry())
        self._origins = ExtensionOrigins()
        # ``ExtensionState`` was written once, at install, and never
        # again — so a service that died ten minutes later left the
        # registry, and therefore the UI, saying READY. This keeps it
        # true, and asks immediately when a call fails rather than
        # waiting out its interval while the panel contradicts what
        # the user just saw.
        self._extension_health = ExtensionHealth(
            registry=self._extensions.registry,
            rpc=self._extensions,
            mcp=self._backend.mcp_manager,
        )
        self._extensions.watch(self._extension_health.on_call_failed)
        # What an extension adds to the *agent* — what it does to the
        # prompts and the agents, its anchors, its commands. Tied to
        # READY rather than to being installed: an extension whose
        # service has died must stop telling the model about data it
        # can no longer fetch. Collisions between extensions are
        # applied as a super admin decided them, from the store.
        extension_store = ExtensionStore()
        self._extension_contributions = ExtensionContributions(
            registry=self._extensions.registry,
            rpc=self._extensions,
            plugins_dir=PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir,
            resolutions=extension_store.resolutions,
            order=extension_store.edit_order,
            igni_commands=IgniCommands.of(CommandHandler.builtin_names()),
        )
        self._extension_contributions.attach()
        # When what extensions do to the prompts changes — one became
        # ready, a condition it reports moved — every session's agent
        # is rebuilt, once, on the next turn of the loop.
        self._agent_refresh = AgentRefresh(loop=loop, refresh=self._refresh_agents)
        prompt_contributions.watch(self._agent_refresh.request)
        # The project's git state, as the host model's workspace
        # signals. Owns the poll, which does no git work until some
        # extension entitled to the workspace is subscribed.
        self._workspace_signals = WorkspaceSignals(project_dir=self._project_dir)
        notifications = self._build_notifications(settings)
        self._notifications = notifications
        self._rpc_router = RpcRouter(
            backend=self._backend,
            transport=self._transport,
            login=self._login,
            push=self._push_bridge,
            extensions=self._extensions,
            origins=self._origins,
            notifications=self._notifications,
        )
        rpc_table = self._rpc_router.build_table()

        # Emit the "ready" line BEFORE background services fire —
        # the FE gates on this line and knowledge load is
        # GIL-heavy, so if we started it first the FE would think
        # we hung.
        self._emit_ready_line()

        # Background services now that the ready line is out.
        self._backend.start_boot_background_services()

        # Scheduler auto-start is idempotent (see
        # ``core.session.scheduler``); if it fails we log and
        # move on — the FE can retry via the ``start_scheduler``
        # RPC.
        try:
            self._push_bridge.start_scheduler(self._backend)
        except Exception:
            logger.exception("Auto-start of scheduler failed; will retry on client RPC")

        # Orchestrator: builds the pool, wires the default runtime,
        # registers itself as the receive-loop consumer.
        self._orchestrator = SessionOrchestrator(
            backend=self._backend,
            transport=self._transport,
            settings=settings,
            project_dir=self._project_dir,
            additional_dirs=self._additional_dirs,
            rpc_router=self._rpc_router,
            rpc_table=rpc_table,
            push_bridge=self._push_bridge,
            login=self._login,
            queue=self._queue,
            extensions=self._extensions,
            origins=self._origins,
            notifications=self._notifications,
        )
        pool = self._orchestrator.setup_pool()
        self._supervisor.set_pool(pool)
        self._start_host_model()
        # The push bridge subscribes to the process-wide shell bus and
        # narrates background-process completions into ITS session's
        # conversation. Once the pool holds more than the boot
        # runtime that narration is addressed to the wrong window, so
        # the bridge needs to know how many sessions are live.
        self._push_bridge.set_live_session_count(lambda: len(pool.runtimes))
        # Workflow runner (CC ``/workflows`` parity): the BE runs
        # ``.claude/workflows/*.mjs`` scripts in a Node subprocess
        # and streams live progress on the ``workflow_event`` push
        # channel. The runner is attached to the default
        # ``BackendServer`` so per-runtime RPC dispatch can find
        # it via ``backend.workflow_runner``.
        from ember_code.backend.workflow_runner import WorkflowRunner

        self._backend.workflow_runner = WorkflowRunner(
            project_dir=self._project_dir,
            push=self._push_bridge,
        )
        self._supervisor.start_evictor()
        self._supervisor.mark_running()

        try:
            if self._ws_port is not None:
                # GUI shells (Tauri / VSCode / JetBrains webviews)
                # may open long after the BE is ready, and webviews
                # reload at will. No timeout here — shutdown comes
                # from signals, the parent watchdog, or an explicit
                # Shutdown message instead.
                await self._transport.wait_for_connection(timeout=None)
            else:
                await self._transport.wait_for_connection()
            await self._start_extensions()
            # Read igni server's notifications and follow its stream;
            # after the client is connected, so the counts this pushes
            # have somebody to reach.
            notifications.start()
            # Probing starts once there is a loop to run it on, and
            # after the installs above, so the first sweep has
            # something to sweep.
            self._extension_health.start()
            # After the installs, for the same reason health is:
            # the first poll asks "is anyone listening", and before
            # this point the answer is always no.
            await self._workspace_signals.start()
            logger.info("FE connected, processing messages")
            await self._orchestrator.serve(self._supervisor.shutdown_event)
        except Exception as exc:
            logger.error("Backend error: %s", exc, exc_info=True)
        finally:
            await notifications.stop()
            if self._host_channel is not None:
                await self._host_channel.stop()
            await self._workspace_signals.stop()
            await self._extension_health.stop()
            if self._distribution is not None:
                await self._distribution.stop()
            if self._service_tokens is not None:
                await self._service_tokens.stop()
            await self._origins.stop_all()
            await self._supervisor.teardown()

    def _build_notifications(self, settings: Settings) -> NotificationCenter:
        """The floor's notifications, both paths (igni server's ``docs/service-identity.md`` §10).

        igni server's half follows the signed-in person, so it starts
        again on every sign-in. The local half is whatever an extension's
        process says with ``notifications/igni/notify`` on its own MCP
        connection — taken out of every stdio connection the manager
        opens, and accepted only as coming from the extension that
        connection belongs to.
        """
        assert self._push_bridge is not None and self._backend is not None
        center = NotificationCenter(
            server=SignedInServer(
                api_url=self._igni_api_url,
                session=CredentialsSession(settings.auth.credentials_file),
            ),
            local=LocalNotificationStore.from_data_dir(settings.storage.data_dir),
            announce=self._push_bridge.notifications_changed,
        )
        receiver = ExtensionNoticeReceiver(registry=self._extensions.registry, sink=center)
        self._notices = receiver
        router = getattr(self._backend.mcp_manager, "notifications", None)
        if router is not None:
            router.handle(ExtensionNoticeReceiver.METHOD, receiver)
            # And what an extension says holds — the ``when`` of its
            # prompt contributions. On the same connections, for the
            # same reason: the connection names the sender.
            router.handle(
                ExtensionConditionReceiver.METHOD,
                ExtensionConditionReceiver(
                    registry=self._extensions.registry,
                    refetch=(
                        self._extension_contributions.refetch
                        if self._extension_contributions is not None
                        else None
                    ),
                ),
            )
        if self._login is not None:
            self._login.on_signed_in(center.schedule_signed_in)
        return center

    def _start_host_model(self) -> None:
        """Build the host object model over the pool, and tell it everything.

        ``docs/HOST_OBJECT_MODEL.md``. One model for every extension,
        reached by a process over :class:`HostChannel` and by a page over
        the ``host_model`` RPC. It hears igni through four taps: every
        outbound message, every window RPC (to signal writes), every
        registry change, and the workspace poll.
        """
        assert self._orchestrator is not None
        model = HostModelWiring.build(
            orchestrator=self._orchestrator,
            transport=self._transport,
            registry=self._extensions.registry,
            project_dir=self._project_dir,
            store=ExtensionStore(),
            notifications=self._notifications,
            notices=self._notices,
            workspace_signals=self._workspace_signals,
        )
        self._host_model = model
        self._host_channel = HostChannel(model)

    async def _start_extensions(self) -> None:
        """Start what is installed, then bring it up to date.

        Two sources, one boot path. What is already in the store —
        extensions igni distributed earlier, and development installs
        ``igni ext install`` put there — starts first, from disk, with
        no network. Then, in the background, the distribution sync asks
        igni server which extensions its approved services ship, and
        installs, upgrades or removes to match (igni server's ``docs/service-identity.md``
        §8). Start-up never waits for that: a machine that cannot reach
        igni runs what it has.

        Failures are logged and skipped. One extension that will not
        start must not stop the backend, and the registry records why
        so the UI can say it rather than the user finding out by
        opening a blank page.
        """
        assert self._backend is not None
        settings = self._backend.settings
        store = ExtensionStore()
        session = CredentialsSession(settings.auth.credentials_file)
        # One exchange, one cache: an extension's process and its page
        # ask for the same token and igni is asked once.
        exchange = ServiceTokenExchange(api_url=self._igni_api_url, session=session)
        self._service_tokens = ServiceTokenEndpoint(exchange)
        if self._orchestrator is not None and self._origins is not None:
            self._orchestrator.attach_page_tokens(
                PageServiceTokens(origins=self._origins, exchange=exchange)
            )
        installer = ExtensionInstaller(
            registry=self._extensions.registry,
            rpc=self._extensions,
            mcp=self._backend.mcp_manager,
            origins=self._origins,
            project_dir=self._project_dir,
            profile=DeploymentProfile.current(),
            # Where a plugin an extension carries goes. The same
            # directory ``igni plugin install`` writes to, because
            # the loader only looks in one place and an extension's
            # agents are not a different kind of agent.
            plugins_dir=PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir,
            allow_development=DevelopmentFlag.enabled(),
            store=store,
            environments=PythonEnvironments(store.environments_root),
            service_tokens=self._service_tokens,
            host_channel=self._host_channel,
        )
        launcher = ExtensionLauncher(
            store=store,
            installer=installer,
            registry=self._extensions.registry,
            contributions=self._extension_contributions,
        )
        await launcher.start_installed()

        self._distribution = ExtensionDistribution(
            store,
            api_url=self._igni_api_url,
            session=session,
            lifecycle=launcher,
            on_resolutions=(
                self._extension_contributions.resolutions_changed
                if self._extension_contributions is not None
                else None
            ),
        )
        self._distribution.schedule()
        # And again whenever somebody signs in: the listing is the
        # signed-in person's, and before that there was none.
        if self._login is not None:
            self._login.on_signed_in(self._distribution.schedule)

    def _refresh_agents(self) -> None:
        """Rebuild every session's agents with the prompts as they are now."""
        if self._orchestrator is not None:
            self._orchestrator.refresh_agents()

    def _igni_api_url(self) -> str | None:
        """igni server's URL as it is configured now — read per call,
        because signing in can change which server that is."""
        return self._backend.settings.api_url if self._backend is not None else None

    def _emit_ready_line(self) -> None:
        """Write the "ready" JSON line to stdout so the parent
        knows how to connect. Serialised via
        :class:`BackendReadyLine` so the wire shape is typed —
        ``model_dump_json(exclude_none=True)`` produces bytes
        byte-compatible with the old ``json.dumps(dict)`` output
        (verified by ``tests/test_backend_ready_line.py``)."""
        ws_transport = self._transport_builder.ws_transport
        ready = BackendReadyLine(status="ready")
        if ws_transport is not None:
            ready.ws_port = ws_transport.port
            ready.ws_url = f"ws://127.0.0.1:{ws_transport.port}"
            # Publish the port + version at
            # ``<project>/.igni/backend.lock`` so a second client
            # opening the same project can discover this BE.
            assert self._supervisor is not None
            self._supervisor.write_discovery_lockfile(ws_transport.port)
        if self._socket_path is not None:
            ready.socket = str(self._socket_path)
        # ``exclude_none`` matches the legacy shape: keys the caller
        # didn't set are omitted rather than serialised as null.
        payload = json.loads(ready.model_dump_json(exclude_none=True))
        print(json.dumps(payload), flush=True)
