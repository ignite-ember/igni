"""Starting an extension's process inside an OS sandbox: the one interface.

:meth:`ExtensionSandbox.confine` takes the stdio MCP server config the
installer built and returns one that runs the same command confined to
the extension's grants (:class:`~.plan.SandboxPlan`), with an egress
proxy (:class:`~.proxy.EgressProxy`) running for it in igni. Every
process igni starts for an extension goes through here
(:meth:`ExtensionInstaller._service_config`'s result is confined before
it is started); an ``endpoint`` extension has no process here and is
left as it is.

What the process sees changes in four ways, all in its environment:

* ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` (and lower case) name the
  proxy, with the run's credentials in the URL; ``NO_PROXY`` keeps
  loopback direct, where igni's host channel,
  token endpoint and the extension's own local database are.
* ``HOME`` is a directory of its run's own, and ``TMPDIR`` (``TMP``,
  ``TEMP``, Java's ``java.io.tmpdir`` through ``JAVA_TOOL_OPTIONS``) its
  run's temp directory — the person's home is not readable, and a tool
  that finds its config there would fail rather than start empty.
* ``IGNI_SANDBOX`` names the sandbox (``seatbelt``, ``bubblewrap``).

Per platform: macOS uses Seatbelt (:mod:`.seatbelt`), Linux bubblewrap
(:mod:`.bubblewrap`), and Windows nothing yet. Where there is no sandbox
the process runs as before, with a warning in the log and on the
extension (:attr:`Confinement.reason`), unless ``extensions.require_sandbox``
is set, which refuses it (:class:`SandboxRefused`). ``extensions.sandbox:
false`` turns all of this off for development
(:class:`~.settings.SandboxSettings`).
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ember_code.core.mcp.config import MCPServerConfig, MCPTransport
from ember_code.core.network_policy import NetworkPolicy
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.permissions import PermissionDenied
from ember_code.extensions.sandbox.bubblewrap import PROXY_PORT, Bubblewrap
from ember_code.extensions.sandbox.plan import SandboxPlan
from ember_code.extensions.sandbox.proxy import Connector, EgressProxy, LoopbackRelay
from ember_code.extensions.sandbox.seatbelt import Seatbelt
from ember_code.extensions.sandbox.settings import SandboxSettings

logger = logging.getLogger(__name__)

#: Told to the process: which sandbox it runs in.
SANDBOX_ENV = "IGNI_SANDBOX"

#: Where the proxy is not used: this machine.
#: Not ``.localhost``: the sandbox has no DNS, so such a name is reached
#: through the proxy, which resolves it outside (RFC 6761: to loopback).
NO_PROXY = "localhost,127.0.0.1,::1"


class SandboxRefused(PermissionDenied):
    """``extensions.require_sandbox`` is set and this machine has no sandbox."""


@dataclass(slots=True)
class Confinement:
    """How one extension's process runs: confined, or not and why."""

    extension_id: str
    #: ``seatbelt`` or ``bubblewrap``; empty when it runs unconfined.
    mechanism: str = ""
    #: Why it runs unconfined; empty when confined.
    reason: str = ""
    #: Unconfined because a setting turned the sandbox off, rather than
    #: because this machine has none.
    deliberate: bool = False
    plan: SandboxPlan | None = None
    proxy: EgressProxy | None = None
    relays: list[LoopbackRelay] = field(default_factory=list)

    @property
    def confined(self) -> bool:
        return bool(self.mechanism)

    def describe(self) -> str:
        """One line for the extensions panel."""
        if self.confined:
            return f"sandboxed ({self.mechanism})"
        return f"not sandboxed: {self.reason}"


class ExtensionSandbox:
    """Wraps extension commands in the platform's sandbox, and runs their proxies."""

    def __init__(
        self,
        *,
        settings: SandboxSettings | None = None,
        network: NetworkPolicy | None = None,
        platform: str | None = None,
        connect: Connector | None = None,
        run_root: Path | None = None,
    ) -> None:
        """
        Args:
            settings: The two switches; read from managed, env and user
                settings by default (never a project's).
            network: The air gap. Without one, nothing is narrowed.
            platform: ``sys.platform`` by default; tests pass another to
                simulate a machine with no sandbox.
            connect: How the proxy opens an upstream connection (tests).
            run_root: Where run directories go; the system temp dir.
        """
        self._settings = settings if settings is not None else SandboxSettings.load()
        self._network = network or NetworkPolicy()
        self._platform = platform or sys.platform
        self._connect = connect
        self._run_root = run_root
        self._probed: tuple[str, str] | None = None
        self._runs: dict[str, Confinement] = {}

    @classmethod
    def for_settings(cls, settings: Any) -> ExtensionSandbox:
        """From igni's loaded settings: its air gap, and the switches from their tiers.

        The organisation's group policy counts for ``require_sandbox``
        only as the pack igni server signed and igni verifies here, the
        same way the settings loader reads it.
        """
        from ember_code.core.config.group_policy import CachedSettingsFetcher

        try:
            group = CachedSettingsFetcher.for_settings(settings)
        except Exception:  # noqa: BLE001 — a partial settings object has no pack
            group = None
        return cls(
            settings=SandboxSettings.load(group=group),
            network=NetworkPolicy.from_settings(settings),
        )

    @property
    def settings(self) -> SandboxSettings:
        return self._settings

    def status(self, extension_id: str) -> Confinement | None:
        return self._runs.get(extension_id)

    async def mechanism(self) -> tuple[str, str]:
        """``(name, "")`` for this machine's sandbox, or ``("", why there is none)``."""
        if self._probed is None:
            self._probed = await self._probe()
        return self._probed

    async def _probe(self) -> tuple[str, str]:
        if self._platform == "darwin":
            reason = await Seatbelt.probe()
            return ("seatbelt", "") if reason is None else ("", reason)
        if self._platform.startswith("linux"):
            reason = await Bubblewrap.probe()
            return ("bubblewrap", "") if reason is None else ("", reason)
        if self._platform in ("win32", "cygwin"):
            return "", "extension processes are not sandboxed on Windows yet"
        return "", f"no sandbox for {self._platform}"

    # ── the one entry point ────────────────────────────────────────

    async def confine(
        self,
        manifest: ExtensionManifest,
        config: MCPServerConfig,
        *,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
        service_url: str | None,
        development: bool,
    ) -> MCPServerConfig:
        """``config`` as it runs in the sandbox, or as it is where it cannot.

        Raises:
            SandboxRefused: No sandbox here and ``require_sandbox`` is set.
        """
        if config.type is not MCPTransport.stdio or not config.command:
            return config
        await self.release(manifest.id)

        if not self._settings.enabled:
            self._runs[manifest.id] = Confinement(
                manifest.id,
                reason=f"extensions.sandbox is false in the {self._settings.disabled_by} settings",
                deliberate=True,
            )
            logger.warning(
                "extension %s runs UNSANDBOXED: extensions.sandbox is false (%s settings)",
                manifest.id,
                self._settings.disabled_by,
            )
            return config

        mechanism, why = await self.mechanism()
        if not mechanism:
            if self._settings.required:
                raise SandboxRefused(
                    f"{manifest.id} was not started: extensions.require_sandbox is set "
                    f"and this machine has no sandbox ({why})."
                )
            self._runs[manifest.id] = Confinement(manifest.id, reason=why)
            logger.warning(
                "extension %s runs UNSANDBOXED, with this person's full file and network "
                "access: %s. Set extensions.require_sandbox in managed settings to refuse "
                "instead.",
                manifest.id,
                why,
            )
            return config

        run_dir = Path(
            tempfile.mkdtemp(prefix=f"igni-ext-{_safe(manifest.id)}-", dir=self._run_root)
        ).resolve()
        try:
            return await self._wrap(
                mechanism,
                manifest,
                config,
                run_dir=run_dir,
                bundle=bundle,
                data_dir=data_dir,
                project_dir=project_dir,
                service_url=service_url,
                development=development,
            )
        except Exception:
            await self.release(manifest.id)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    async def _wrap(
        self,
        mechanism: str,
        manifest: ExtensionManifest,
        config: MCPServerConfig,
        *,
        run_dir: Path,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
        service_url: str | None,
        development: bool,
    ) -> MCPServerConfig:
        env = dict(config.env or {})
        cwd = Path(config.cwd) if config.cwd else None
        plan = SandboxPlan.build(
            manifest,
            command=config.command or "",
            env=env,
            cwd=cwd,
            bundle=bundle,
            data_dir=data_dir,
            project_dir=project_dir,
            service_url=service_url,
            development=development,
            network=self._network,
            run_dir=run_dir,
        )
        plan.home_dir.mkdir(parents=True, exist_ok=True)
        if plan.withheld:
            logger.warning(
                "extension %s: %s withheld from its sandbox — outside this air-gapped network",
                manifest.id,
                ", ".join(plan.withheld),
            )

        if plan.plain_http_service:
            logger.warning(
                "extension %s: its service %s is plain http, so its process may not reach "
                "it. Serve the service over https, or, developing on this machine, start "
                "igni with IGNI_DEV_EXTENSIONS=1.",
                manifest.id,
                plan.plain_http_service,
            )

        proxy = EgressProxy(
            manifest.id, plan.hosts, connect=self._connect, token=secrets.token_urlsafe(24)
        )
        run = Confinement(manifest.id, mechanism=mechanism, plan=plan, proxy=proxy)
        self._runs[manifest.id] = run

        if mechanism == "seatbelt":
            port = await proxy.start_tcp()
            granted = {port, *plan.loopback_ports}
            plan.denied_ports = sorted((await Seatbelt.listening_ports()) - granted)
            command, args = Seatbelt.command(plan, config.command or "", list(config.args or []))
        else:
            Bubblewrap.socket_dir(plan).mkdir(mode=0o700, exist_ok=True)
            await proxy.start_unix(Bubblewrap.proxy_socket(plan))
            for loop_port in plan.loopback_ports:
                relay = LoopbackRelay(loop_port, Bubblewrap.relay_socket(plan, loop_port))
                await relay.start()
                run.relays.append(relay)
            port = PROXY_PORT
            command, args = Bubblewrap.command(
                plan, config.command or "", list(config.args or []), cwd=cwd
            )

        env.update(self._environment(plan, proxy.url(port), mechanism, env))
        logger.info(
            "extension %s sandboxed (%s): reads %d paths, writes %s, reaches %s",
            manifest.id,
            mechanism,
            len(plan.read),
            ", ".join(str(p) for p in plan.write),
            ", ".join(plan.hosts.entries) or "no host beyond this machine",
        )
        return config.model_copy(update={"command": command, "args": args, "env": env})

    @staticmethod
    def _environment(
        plan: SandboxPlan, proxy: str, mechanism: str, env: dict[str, str]
    ) -> dict[str, str]:
        tmp = str(plan.temp_dir)
        java = env.get("JAVA_TOOL_OPTIONS", "")
        out = {
            SANDBOX_ENV: mechanism,
            "HOME": str(plan.home_dir),
            "TMPDIR": tmp,
            "TMP": tmp,
            "TEMP": tmp,
            "JAVA_TOOL_OPTIONS": f"{java} -Djava.io.tmpdir={tmp}".strip(),
        }
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            out[name] = proxy
            out[name.lower()] = proxy
        out["NO_PROXY"] = out["no_proxy"] = NO_PROXY
        return out

    async def release(self, extension_id: str) -> None:
        """Stop its proxy and relays and remove its run directory."""
        run = self._runs.pop(extension_id, None)
        if run is None:
            return
        if run.proxy is not None:
            with contextlib.suppress(Exception):
                await run.proxy.stop()
        for relay in run.relays:
            with contextlib.suppress(Exception):
                await relay.stop()
        if run.plan is not None and run.plan.run_dir is not None:
            shutil.rmtree(run.plan.run_dir, ignore_errors=True)

    async def close(self) -> None:
        for extension_id in list(self._runs):
            await self.release(extension_id)


def _safe(extension_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in extension_id)[:40]


__all__ = ["NO_PROXY", "SANDBOX_ENV", "Confinement", "ExtensionSandbox", "SandboxRefused"]
