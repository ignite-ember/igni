"""Turning a bundle on disk into something that answers.

This is the keystone the rest of the extension system was waiting
on. The manifest was modelled, the namespace was routed and
authorised, the frame was built and the floor was published — and
nothing anywhere called ``ExtensionRegistry.install``, so every one
of those was reachable only from a test.

What an install does, in order, and why that order:

1. **Read and validate the manifest.** Anything wrong here is wrong
   before we have touched the machine.
2. **Check trust.** An unsigned bundle is refused unless the
   deployment opted in. This is the point at which a signature would
   be verified; it is not verified yet, and
   :meth:`ExtensionInstaller.install` says so out loud rather than
   implying otherwise by succeeding.
3. **Register as PREPARING.** From here on a failure is *recorded*
   rather than raised away: an extension that failed to start is a
   state an administrator needs to see, with the reason and the fix,
   not an exception in a log nobody reads.
4. **Start or reach the service**, then bind it.
5. **READY**, which is what makes it callable — the registry refuses
   anything else.

Three packagings, one per manifest. ``dev`` runs a command here,
``endpoint`` reaches a service somebody else runs, and ``sidecar``
needs a container runtime this does not have yet and fails saying so.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ember_code.core.mcp.config import MCPServerConfig, MCPTransport
from ember_code.extensions.manifest import ExtensionManifest, Trust, Workspace
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.permissions import PermissionDenied, Permissions
from ember_code.extensions.profile import DeploymentProfile
from ember_code.extensions.registry import (
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import McpExtensionService
from ember_code.extensions.signature import SignatureError, TrustStore, verify_bundle

logger = logging.getLogger(__name__)

#: What an extension's MCP server is called inside the MCP manager.
#:
#: Prefixed so an extension cannot shadow a server the user declared
#: in their own ``.mcp.json`` — the id alone would let an install
#: silently take over ``github``.
SERVER_PREFIX = "ext:"


class InstallError(Exception):
    """The bundle will not be installed, and why.

    Raised for everything decided *before* the extension is
    registered. After that point a failure is recorded as
    :attr:`~ember_code.extensions.registry.ExtensionState.FAILED`
    instead, because an extension that exists and is broken is a
    different thing from one that was never accepted.
    """


def server_name(extension_id: str) -> str:
    """The MCP server name for an extension."""
    return f"{SERVER_PREFIX}{extension_id}"


class ExtensionInstaller:
    """Installs, starts and removes extensions."""

    def __init__(
        self,
        *,
        registry: ExtensionRegistry,
        rpc: ExtensionRpc,
        mcp: Any,
        origins: ExtensionOrigins | None = None,
        trust_store: TrustStore | None = None,
        project_dir: Path | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        profile: DeploymentProfile | None = None,
        allow_unsigned: bool = False,
    ) -> None:
        """
        Args:
            mcp: The :class:`~ember_code.core.mcp.client.MCPClientManager`.
                Untyped to keep this module importable without the
                optional ``mcp`` extra, which a headless deployment
                may not have installed.
            origins: Where an extension's UI is served from. Each
                gets its own loopback port, because a port is part of
                an origin and a path prefix is not — two extensions
                sharing one origin could read each other's storage
                and script each other's frames. Optional: an
                extension that contributes no pages needs none, and
                neither does a headless deployment.
            trust_store: The public keys this deployment accepts a
                bundle from. Never populated from the bundle being
                installed, which would make a signature a formality
                any bundle could satisfy by bringing its own key.
            project_dir: The user's project. Told to a service only
                when ``permissions.workspace`` grants it.
            secret_resolver: The deployment's secret source. A name
                the manifest did not declare is never passed to it.
            profile: How many principals this backend serves. A
                sidecar is one process, so a shared backend refuses
                one — see :mod:`ember_code.extensions.profile`.
            allow_unsigned: Whether ``trust: unsigned`` may be
                installed. False everywhere but development, because
                the easiest way to ship must not be the one with no
                provenance.
        """
        self._registry = registry
        self._rpc = rpc
        self._mcp = mcp
        self._origins = origins
        self._trust = trust_store if trust_store is not None else TrustStore()
        self._project_dir = project_dir
        self._secret_resolver = secret_resolver
        self._profile = profile if profile is not None else DeploymentProfile.current()
        self._allow_unsigned = allow_unsigned

    async def install(self, bundle: Path) -> InstalledExtension:
        """Install the bundle at ``bundle`` and try to start it.

        Returns the registry entry either way. A started extension is
        ``READY``; one that could not start is ``FAILED`` with a
        reason, which is a state, not an error.

        Raises:
            InstallError: The bundle is not installable at all — bad
                manifest, refused trust, or an id already installed.
        """
        manifest = self._read(bundle)
        self._check_trust(manifest, bundle)
        self._check_profile(manifest)
        self._check_deliverable(manifest)

        if self._registry.get(manifest.id) is not None:
            raise InstallError(
                f"{manifest.id} is already installed. Remove it first — "
                "an upgrade keeps the old grants until an administrator "
                "accepts the new ones, and that is a separate decision."
            )

        entry = self._registry.install(
            manifest,
            state=ExtensionState.PREPARING,
            reason="starting its service",
        )

        permissions = Permissions(manifest, secret_resolver=self._secret_resolver)

        try:
            config = self._service_config(manifest, bundle, permissions)
        except (InstallError, PermissionDenied) as exc:
            # Packaging we cannot honour. Recorded rather than raised:
            # the extension is installed and not runnable here, which
            # is exactly what an administrator needs to be shown.
            self._registry.set_state(manifest.id, ExtensionState.FAILED, str(exc))
            return entry

        reason = await self._start(config)
        if reason is not None:
            self._registry.set_state(manifest.id, ExtensionState.FAILED, reason)
            return entry

        self._restrict_tools(manifest, config.name, permissions)

        self._rpc.bind(
            manifest.id,
            McpExtensionService(manager=self._mcp, server_name=config.name),
        )

        ui_reason = await self._serve_ui(manifest, bundle)
        if ui_reason is not None:
            # The service works and the UI cannot be shown. FAILED
            # rather than READY: a page in the sidebar that opens on
            # nothing is worse than one that is not offered.
            self._rpc.unbind(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, ui_reason)
            return entry

        self._registry.set_state(manifest.id, ExtensionState.READY)
        logger.info("extension %s is READY", manifest.id)
        return entry

    async def _serve_ui(self, manifest: ExtensionManifest, bundle: Path) -> str | None:
        """Put this extension's pages on their own origin.

        Returns a reason on failure, or ``None`` when there was
        nothing to serve or it was served.
        """
        if not manifest.contributes.pages:
            return None
        if self._origins is None:
            return (
                f"{manifest.id} contributes pages and this backend serves no "
                "extension UI, so they could not be shown."
            )
        root = bundle / "ui"
        if not root.is_dir():
            return (
                f"{manifest.id} contributes pages but the bundle has no 'ui' "
                f"directory at {root}, so there is nothing to serve."
            )
        try:
            await self._origins.serve(
                manifest.id, root, connect_src=self._egress_connect_src(manifest)
            )
        except Exception as exc:
            logger.error("serving %s UI failed", manifest.id, exc_info=True)
            return f"its UI could not be served: {exc}"
        return None

    async def uninstall(self, extension_id: str) -> bool:
        """Stop and forget an extension. ``True`` if there was one.

        Order matters here too, in reverse: unbind first so nothing
        new is routed, then stop the service, then forget it. Doing
        it the other way leaves a window where a call is authorised
        against a registry entry whose service has already gone.
        """
        if self._registry.get(extension_id) is None:
            return False
        self._rpc.unbind(extension_id)
        if self._origins is not None:
            await self._origins.drop(extension_id)
        try:
            await self._mcp.disconnect_one(server_name(extension_id))
        except Exception as exc:  # pragma: no cover — best effort
            logger.warning("stopping %s failed: %s", extension_id, exc)
        self._registry.remove(extension_id)
        logger.info("extension %s removed", extension_id)
        return True

    # ── The steps ──────────────────────────────────────────────────

    @staticmethod
    def _read(bundle: Path) -> ExtensionManifest:
        path = bundle / "manifest.json"
        if not path.is_file():
            raise InstallError(f"{bundle} has no manifest.json, so there is nothing to install.")
        try:
            return ExtensionManifest.load(path)
        except ValueError as exc:
            raise InstallError(f"{path} is not a valid manifest: {exc}") from exc

    def _check_trust(self, manifest: ExtensionManifest, bundle: Path) -> None:
        """Provenance, before anything is registered or started.

        An unsigned bundle is refused outright unless the deployment
        opted in. A signed one has its signature checked against the
        keys an administrator added — offline, because an air-gapped
        customer is the normal case and a check that needs the
        network is a check that gets skipped.
        """
        if manifest.trust is Trust.UNSIGNED:
            if not self._allow_unsigned:
                raise InstallError(
                    f"{manifest.id} is unsigned and this deployment does not accept "
                    "unsigned extensions. Sign it, or enable development mode."
                )
            logger.warning("extension %s is unsigned; this deployment allows that", manifest.id)
            return

        try:
            key = verify_bundle(bundle, manifest, self._trust)
        except SignatureError as exc:
            raise InstallError(f"{manifest.id}: {exc}") from exc
        logger.info("extension %s signed by %s", manifest.id, key.owner)

    def _check_profile(self, manifest: ExtensionManifest) -> None:
        """What this deployment's shape forbids.

        Both rules have been in EXTENSIBILITY since it was written
        and neither was checked, so an administrator could install
        the thing the document says does not work and find out from
        the behaviour.
        """
        if not self._profile.is_shared:
            return

        if manifest.sidecar is not None or manifest.dev is not None:
            raise InstallError(
                f"{manifest.id} is packaged to run as a process here, and this "
                "backend serves more than one principal. One process would be "
                "one instance shared by all of them, with a single workspace "
                "and a single resolution of each secret — there is no answer "
                "to whose. Use 'endpoint' packaging, which is a service that "
                "can be multi-tenant."
            )

        if manifest.contributes.pages:
            raise InstallError(
                f"{manifest.id} contributes pages and this backend serves more "
                "than one principal. A frame is hosted by the igni app beside "
                "the user, and a shared backend has no such app — the pages "
                "would be served and never shown."
            )

    @staticmethod
    def _check_deliverable(manifest: ExtensionManifest) -> None:
        """Refuse a grant this packaging cannot deliver.

        ``workspace`` and ``secrets`` reach a service through its
        environment, and igni only sets an environment for a process
        it starts. A registered endpoint is somebody else's process:
        the grant is declared, approved by an administrator, and
        then silently does nothing — which is the failure this whole
        system has been removing, so it is refused rather than
        ignored.
        """
        if manifest.endpoint is None:
            return
        undeliverable = []
        if manifest.permissions.workspace is not Workspace.NONE:
            undeliverable.append(f"workspace: {manifest.permissions.workspace.value}")
        if manifest.permissions.secrets:
            undeliverable.append(f"secrets: {list(manifest.permissions.secrets)}")
        if not undeliverable:
            return
        raise InstallError(
            f"{manifest.id} declares {' and '.join(undeliverable)}, and igni "
            "delivers those in the environment of a process it starts. This is "
            "an endpoint — somebody else's process — so the grant would be "
            "approved and never arrive. Remove it, or have the service read "
            "its own configuration."
        )

    def _service_config(
        self, manifest: ExtensionManifest, bundle: Path, permissions: Permissions
    ) -> MCPServerConfig:
        """How to reach this extension's service, within its grants."""
        name = server_name(manifest.id)

        if manifest.dev is not None:
            cwd = bundle if manifest.dev.cwd is None else bundle / manifest.dev.cwd
            # The manifest's own `env` first, so a grant cannot be
            # shadowed by a literal the bundle shipped — an extension
            # setting `IGNI_WORKSPACE` itself must not be how it gets
            # one.
            env = dict(manifest.dev.env)
            env.update(permissions.service_environment(self._project_dir))
            return MCPServerConfig(
                name=name,
                type=MCPTransport.stdio,
                command=manifest.dev.command[0],
                args=list(manifest.dev.command[1:]),
                env=env,
                # Its command is relative to the bundle, which is not
                # where the backend happens to be running.
                cwd=str(cwd),
                source="extension",
            )

        if manifest.endpoint is not None:
            # igni makes this connection itself, so igni can refuse a
            # destination the manifest never declared.
            permissions.egress.check_endpoint(manifest.endpoint.url)
            return MCPServerConfig(
                name=name,
                type=MCPTransport.sse,
                url=manifest.endpoint.url,
                source="extension",
            )

        raise InstallError(
            f"{manifest.id} is packaged as a sidecar image "
            f"({manifest.sidecar.image if manifest.sidecar else '?'}), and "
            "starting container images is not implemented yet. Use 'endpoint' "
            "to register a service you run, or 'dev' to run a command locally."
        )

    @staticmethod
    def _egress_connect_src(manifest: ExtensionManifest) -> str:
        return Permissions(manifest).egress.csp_connect_src()

    def _restrict_tools(
        self, manifest: ExtensionManifest, server: str, permissions: Permissions
    ) -> None:
        """Keep undeclared tools away from the agent.

        Complete enforcement, unlike most of this file: registration
        is igni's, so an extension's service may expose whatever it
        likes and what reaches the model is the manifest's list.

        Done through the same filter a user's own disabling uses,
        because the end state is identical — a tool the agent is not
        offered — and a second mechanism would be a second thing to
        keep in step.
        """
        client = self._mcp.get_client(server)
        if client is None:
            return
        offered = self._mcp.get_tools(server)
        refused = permissions.tools.refused(offered)
        for tool in refused:
            self._mcp.set_tool_enabled(server, tool, False)
        if refused:
            logger.info(
                "extension %s offered %d tools its manifest does not declare; not registering %s",
                manifest.id,
                len(refused),
                refused,
            )

    async def _start(self, config: MCPServerConfig) -> str | None:
        """Connect to the service. Returns a reason on failure."""
        try:
            self._mcp.configs[config.name] = config
            result = await self._mcp.connect(config.name)
        except Exception as exc:
            logger.error("extension service %s failed to start", config.name, exc_info=True)
            return f"its service could not be started: {exc}"

        if not getattr(result, "ok", False):
            reason = getattr(result, "reason", "") or "no reason given"
            return f"its service did not start: {reason}"
        return None
