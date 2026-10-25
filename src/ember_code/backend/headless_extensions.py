"""Extensions in a headless run (``-m``, ``--pipe``).

An extension's backend logic runs in every mode; only its UI needs an
app to load into. A headless run is one session, one message, no
window — so it starts the extensions installed on this machine the way
the app does (:meth:`~ember_code.backend.app.BackendApp._start_extensions`,
without the distribution sync), and their tools, hooks and prompt
contributions apply to it. Without this a policy extension's
``PreToolUse`` hook could be stepped around by running igni with ``-m``.

The host model is there for the processes to connect to — it carries
the hook events — but there is no window behind it: a member that would
act on one is refused, and says why.

Starting is bounded (:attr:`HeadlessExtensions.START_TIMEOUT_S`). An
extension that did not come up in time is ``FAILED``, and a fail-closed
hook it holds (``PreToolUse``, ``UserPromptSubmit``) refuses its events
with that reason rather than letting them through unchecked.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.hooks.extension_gate import ExtensionHookGate
from ember_code.core.plugins.installer import PluginInstaller
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.host.channel import HostChannel
from ember_code.extensions.host.errors import HostModelError
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.model import HostModel
from ember_code.extensions.host.spec import ErrorCode
from ember_code.extensions.impact import IgniCommands
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.launcher import ExtensionLauncher
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.principal import SignedInPerson
from ember_code.extensions.profile import DeploymentProfile, DevelopmentFlag
from ember_code.extensions.python_env import PythonEnvironments
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import (
    CredentialsSession,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
)
from ember_code.extensions.store import ExtensionStore
from ember_code.protocol.messages import Message
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


class NoWindowState:
    """What windows remember, in a run that has none: nothing."""

    def get_for_client(self, client_id: str) -> dict[str, str]:
        return {}

    def set_value(self, client_id: str, key: str, value: str) -> None:
        return None

    def delete_value(self, client_id: str, key: str) -> None:
        return None


class HeadlessHostBackend:
    """The host model's backend in a run with no window: nothing to act on."""

    def __init__(self, project_dir: Path, session_id: str) -> None:
        self.project_dir = project_dir
        self.client_state = NoWindowState()
        self._session_id = session_id

    @staticmethod
    def _refused(what: str) -> HostModelError:
        return HostModelError(ErrorCode.REFUSED, f"{what}: this is a headless run, with no window")

    def default_session(self) -> str:
        return self._session_id

    def live_sessions(self) -> list[str]:
        return [self._session_id]

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        raise self._refused(method.value)

    async def act(self, message: Message, *, session_id: str) -> None:
        raise self._refused(type(message).__name__)

    async def ask(self, message: Message, *, session_id: str, timeout: float = 120.0) -> Message:
        raise self._refused(type(message).__name__)

    async def attach(self, session_id: str, project_dir: str | None) -> dict[str, str]:
        raise self._refused("attach_session")

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        queued: bool,
    ) -> None:
        raise self._refused("chat.post")

    async def show_view(self, *, session_id: str, payload: dict[str, Any], run_id: str) -> None:
        raise self._refused("chat.show")

    def views(self, session_id: str) -> list[dict[str, Any]]:
        return []

    def project_dir_of(self, session_id: str) -> Path:
        return self.project_dir

    def broadcast(self, message: Message) -> None:
        return None


class HeadlessExtensions:
    """Starts this machine's installed extensions for one headless run, and stops them."""

    #: How long the run waits for its extensions to come up.
    START_TIMEOUT_S = 60.0

    def __init__(self, session: Any) -> None:
        self._session = session
        self._registry = ExtensionRegistry()
        self._channel: HostChannel | None = None

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    async def start(self) -> None:
        """Start what is installed, bounded; then rebuild the agents with what they add."""
        session = self._session
        settings = session.settings
        store = ExtensionStore()
        if not store.installed():
            return
        rpc = ExtensionRpc(self._registry)
        plugins_dir = PluginInstaller(data_dir=settings.storage.data_dir).plugins_dir
        contributions = ExtensionContributions(
            registry=self._registry,
            rpc=rpc,
            plugins_dir=plugins_dir,
            priority=store.priority,
            igni_commands=IgniCommands.of(CommandHandler.builtin_names()),
        )
        contributions.attach()
        model = HostModel(
            backend=HeadlessHostBackend(Path(session.project_dir), str(session.session_id)),
            registry=self._registry,
            store=store,
        )
        self._channel = HostChannel(model)
        hooks = ExtensionHooks.wire(self._registry)
        ExtensionHookGate.install(hooks)
        person = SignedInPerson(settings.auth.credentials_file, store)
        session.mcp_manager.extension_request_meta = person.meta
        hooks.set_principal(person)
        exchange = ServiceTokenExchange(
            api_url=lambda: settings.api_url,
            session=CredentialsSession(settings.auth.credentials_file),
        )
        installer = ExtensionInstaller(
            registry=self._registry,
            rpc=rpc,
            mcp=session.mcp_manager,
            origins=ExtensionOrigins(),
            project_dir=Path(session.project_dir),
            profile=DeploymentProfile.current(),
            plugins_dir=plugins_dir,
            allow_development=DevelopmentFlag.enabled(),
            store=store,
            environments=PythonEnvironments(store.environments_root),
            service_tokens=ServiceTokenEndpoint(exchange, principal=person),
            host_channel=self._channel,
        )
        launcher = ExtensionLauncher(
            store=store, installer=installer, registry=self._registry, contributions=contributions
        )
        try:
            await asyncio.wait_for(launcher.start_installed(), self.START_TIMEOUT_S)
        except TimeoutError:
            for extension_id in self._registry.ids():
                entry = self._registry.get(extension_id)
                if entry is not None and entry.state is ExtensionState.PREPARING:
                    self._registry.set_state(
                        extension_id,
                        ExtensionState.FAILED,
                        f"it did not start within {self.START_TIMEOUT_S:g}s",
                    )
        for extension_id in self._registry.ids():
            entry = self._registry.get(extension_id)
            if entry is not None and entry.state is not ExtensionState.READY:
                logger.warning("extension %s is %s: %s", extension_id, entry.state, entry.reason)
        session.reload_group_agents()

    async def stop(self) -> None:
        """Stop the extensions' processes, and take their hooks out of the gate."""
        if self._channel is None:
            return
        ExtensionHookGate.install(None)
        await self._session.mcp_manager.disconnect_all()
        await self._channel.stop()


__all__ = ["HeadlessExtensions", "HeadlessHostBackend", "NoWindowState"]
