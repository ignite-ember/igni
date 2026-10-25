"""Turning a bundle on disk into something that answers.

This is the keystone the rest of the extension system was waiting
on. The manifest was modelled, the namespace was routed and
authorised, the frame was built and the floor was published — and
nothing anywhere called ``ExtensionRegistry.install``, so every one
of those was reachable only from a test.

What an install does, in order, and why that order:

1. **Read and validate the manifest.** Anything wrong here is wrong
   before we have touched the machine.
2. **Check trust**, which is a question about where the bundle came
   from. One igni server distributed — its signature verified against
   the approved service's key before it was stored — is trusted. Any
   other is a development install, refused unless the person at this
   machine set ``IGNI_DEV_EXTENSIONS``.
3. **Register as PREPARING.** From here on a failure is *recorded*
   rather than raised away: an extension that failed to start is a
   state an administrator needs to see, with the reason and the fix,
   not an exception in a log nobody reads.
4. **Start or reach the service**, then bind it.
5. **READY**, which is what makes it callable — the registry refuses
   anything else.

Four packagings, one per manifest. ``python`` installs a package into
a private environment and runs it here, ``dev`` runs a command here,
``endpoint`` reaches a service somebody else runs, and ``sidecar``
needs a container runtime this does not have yet and fails saying so.

A process igni starts that belongs to a service is told how to get a
token for it — ``IGNI_SERVICE_URL``, ``IGNI_SERVICE_TOKEN_URL`` and
``IGNI_SERVICE_TOKEN_SECRET`` (:mod:`ember_code.extensions.service_token`).
It never sees the person's igni session.
"""

from __future__ import annotations

import logging
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.core.agents.tool_audience import ToolAudience
from ember_code.core.agents.tool_audience import audience as default_audience
from ember_code.core.mcp.config import MCPServerConfig, MCPTransport
from ember_code.core.mcp.naming import bare_tool_name
from ember_code.extensions.capabilities import (
    CapabilityResolver,
    Connections,
    Resolved,
    UnresolvedRequirement,
    check_dimensions_agree,
)
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.manifest import ExtensionManifest, Workspace
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.permissions import PermissionDenied, Permissions, ServiceOrigin
from ember_code.extensions.profile import DEV_FLAG, DeploymentProfile
from ember_code.extensions.python_env import PythonEnvironmentError, PythonEnvironments
from ember_code.extensions.registry import (
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import McpExtensionService
from ember_code.extensions.service_token import ServiceTokenEndpoint
from ember_code.extensions.store import DistributionRecord, ExtensionStore
from ember_code.extensions.web_home import SERVICE_URL_SETTING

if TYPE_CHECKING:
    from ember_code.extensions.host.channel import HostChannel

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
        project_dir: Path | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        profile: DeploymentProfile | None = None,
        resolver: CapabilityResolver | None = None,
        plugins_dir: Path | None = None,
        audience: ToolAudience | None = None,
        allow_development: bool = False,
        store: ExtensionStore | None = None,
        environments: PythonEnvironments | None = None,
        service_tokens: ServiceTokenEndpoint | None = None,
        host_channel: HostChannel | None = None,
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
            project_dir: The user's project. Told to a service only
                when ``permissions.workspace`` grants it.
            secret_resolver: The deployment's secret source. A name
                the manifest did not declare is never passed to it.
            resolver: Turns a declared ``requires`` into a connection.
                Optional: a deployment with no infrastructure to
                offer refuses an extension that needs some, which is
                the honest answer and what ``None`` produces.
            profile: How many principals this backend serves. A
                sidecar is one process, so a shared backend refuses
                one — see :mod:`ember_code.extensions.profile`.
            allow_development: Whether a bundle igni server did not
                distribute may be installed — ``IGNI_DEV_EXTENSIONS``.
                False everywhere but development, because the easiest
                way to ship must not be the one with no provenance.
            store: Where a development install's settings are read,
                for the ``serviceUrl`` its token is fetched for.
            environments: Where ``python`` packaging is installed.
                Without one such an extension cannot start, and says so.
            service_tokens: The loopback endpoint an extension fetches
                its service's token from. Without one, no extension is
                told how to reach its service as the signed-in person.
            host_channel: The loopback socket an extension's process
                reaches the host object model on (``ext.host``). Without
                one, a process is not told where igni is.
        """
        self._registry = registry
        self._rpc = rpc
        self._mcp = mcp
        self._origins = origins
        self._project_dir = project_dir
        self._secret_resolver = secret_resolver
        self._profile = profile if profile is not None else DeploymentProfile.current()
        self._resolver = resolver
        self._allow_development = allow_development
        self._store = store
        self._environments = environments
        self._service_tokens = service_tokens
        self._host_channel = host_channel
        # Where a plugin an extension carries is installed.
        # ``None`` means this deployment does not take them —
        # the extension still installs, and its agents do not
        # arrive, which is said at install rather than found out.
        self._plugins_dir = plugins_dir
        self._audience = audience or default_audience

    async def install(
        self, bundle: Path, *, distribution: DistributionRecord | None = None
    ) -> InstalledExtension:
        """Install the bundle at ``bundle`` and try to start it.

        Returns the registry entry either way. A started extension is
        ``READY``; one that could not start is ``FAILED`` with a
        reason, which is a state, not an error.

        Args:
            distribution: How igni server distributed this bundle, when
                it did. ``None`` makes it a development install.

        Raises:
            InstallError: The bundle is not installable at all — bad
                manifest, refused trust, or an id already installed.
        """
        manifest = self._read(bundle)
        self._check_trust(manifest, distribution)
        self._check_profile(manifest)
        self._check_deliverable(manifest)
        self._check_host_grants(manifest)
        self._check_tool_audience(manifest)

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

        permissions = Permissions(
            manifest,
            secret_resolver=self._secret_resolver,
            development=self._allow_development,
        )
        # Its own service: where its process's token is for, and the one
        # origin beyond declared egress its page may reach.
        service_url = self._service_url(manifest, distribution)

        try:
            resolved = await self._resolve_requirements(manifest)
            entry_point = await self._python_entry(manifest, bundle, distribution)
            token_env = await self._process_grants(manifest, service_url)
            config = self._service_config(
                manifest, bundle, permissions, resolved, entry_point, token_env
            )
        except (
            InstallError,
            PermissionDenied,
            UnresolvedRequirement,
            PythonEnvironmentError,
        ) as exc:
            # Packaging we cannot honour. Recorded rather than raised:
            # the extension is installed and not runnable here, which
            # is exactly what an administrator needs to be shown.
            self._revoke_token(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, str(exc))
            return entry

        reason = await self._start(config)
        if reason is not None:
            self._revoke_token(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, reason)
            return entry

        self._restrict_tools(manifest, config.name, permissions)

        self._rpc.bind(
            manifest.id,
            McpExtensionService(manager=self._mcp, server_name=config.name),
        )

        ui_reason = await self._serve_ui(manifest, bundle, permissions, service_url)
        if ui_reason is not None:
            # The service works and the UI cannot be shown. FAILED
            # rather than READY: a page in the sidebar that opens on
            # nothing is worse than one that is not offered.
            self._rpc.unbind(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, ui_reason)
            return entry

        self._install_plugin(manifest, bundle)
        self._apply_tool_audience(manifest)

        self._registry.set_state(manifest.id, ExtensionState.READY)
        logger.info("extension %s is READY", manifest.id)
        return entry

    async def _serve_ui(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        permissions: Permissions,
        service_url: str | None,
    ) -> str | None:
        """Put this extension's pages on their own origin.

        Its CSP's ``connect-src`` is the page itself, its own service
        and its declared egress — nothing else
        (:meth:`~ember_code.extensions.permissions.Egress.csp_connect_src`).
        The service is recorded on the origin too, as the one audience
        the page's ``bridge.service`` token is fetched for.

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
            service = ServiceOrigin.parse(service_url, development=self._allow_development)
            await self._origins.serve(
                manifest.id,
                root,
                connect_src=permissions.egress.csp_connect_src(service),
                # The URL as it is, not the derived origin: it is the
                # token's audience, and must be the process's exactly.
                service_url=service_url if service is not None else None,
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
        self._remove_plugin(extension_id)
        self._audience.release(server_name(extension_id))
        self._revoke_token(extension_id)
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

    def _check_trust(
        self, manifest: ExtensionManifest, distribution: DistributionRecord | None
    ) -> None:
        """Provenance, before anything is registered or started.

        Where the bundle came from decides, not what its manifest says
        about itself. A distributed one was verified against its
        service's key before it was stored, so here it only has to be
        the bundle that was verified, and not a ``dev`` command; its
        ``trust`` label is not consulted. Any other bundle is a
        development install, and needs the flag the person at this
        machine sets for themselves.
        """
        if distribution is not None:
            if (manifest.id, manifest.version) != (
                distribution.extension_id,
                distribution.version,
            ):
                raise InstallError(
                    f"{manifest.id} {manifest.version} is not what igni distributed "
                    f"({distribution.extension_id} {distribution.version}); the bundle "
                    "changed after it was verified. It is fetched again at the next sync."
                )
            if manifest.is_development_command:
                raise InstallError(
                    f"{manifest.id} uses 'dev' packaging, a command out of its "
                    "manifest, and igni does not run one that came from a service. "
                    "A service ships 'python' packaging."
                )
            logger.info(
                "extension %s %s distributed by igni for %s",
                manifest.id,
                manifest.version,
                distribution.service_url,
            )
            return

        if not self._allow_development:
            raise InstallError(
                f"{manifest.id} was not distributed by igni server, so it is a "
                f"development install, and {DEV_FLAG} is not set. Extensions reach "
                "this machine from their services through igni; set "
                f"{DEV_FLAG}=1 to run one you are developing."
            )
        logger.warning(
            "extension %s is a development install (trust %s); %s allows that",
            manifest.id,
            manifest.trust.value,
            DEV_FLAG,
        )

    def _check_profile(self, manifest: ExtensionManifest) -> None:
        """What this deployment's shape forbids.

        Both rules have been in EXTENSIBILITY since it was written
        and neither was checked, so an administrator could install
        the thing the document says does not work and find out from
        the behaviour.
        """
        if not self._profile.is_shared:
            return

        if manifest.runs_here:
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
    def _check_host_grants(manifest: ExtensionManifest) -> None:
        """Say which ``permissions.host`` entries name nothing the model has.

        Noted, never refused: a manifest written against a newer igni asks
        for objects this one does not have yet.
        ``workspace`` is named specifically, because it reads like a
        host grant and is decided by ``permissions.workspace``.
        """
        stray = HostGrants.unknown(list(manifest.permissions.host), HOST_SCHEMA.permission_names())
        if stray:
            logger.warning(
                "extension %s asks for host-model objects igni does not have: %s. "
                "host.workspace is granted by permissions.workspace, not here.",
                manifest.id,
                ", ".join(stray),
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

    async def _resolve_requirements(self, manifest: ExtensionManifest) -> list[Resolved]:
        """Infrastructure this extension declared, made to exist.

        At install, never later: an extension that starts and then
        discovers it has no database fails in front of a user
        instead of in front of the administrator who installed it.

        A resolver always hands back a *connection*. On a laptop it
        provisioned one first; in a deployment it bound to one the
        infrastructure already runs. The extension cannot tell, and
        that is what lets the same bundle install in both.
        """
        requires = manifest.requires
        if not requires:
            return []
        if self._resolver is None:
            raise UnresolvedRequirement(
                f"{manifest.id} needs "
                f"{sorted(r.capability.value for r in requires)} and this "
                "backend has no capability resolver, so there is nothing to "
                "satisfy it with."
            )
        # Before anything is resolved: an extension whose vector
        # kinds disagree about width is broken whichever instance
        # satisfies them.
        check_dimensions_agree(list(requires), extension_id=manifest.id)

        resolved: list[Resolved] = []
        for requirement in requires:
            answer = await self._resolver.resolve(requirement, extension_id=manifest.id)
            logger.info(
                "extension %s: %s %s (%s)",
                manifest.id,
                requirement.capability.value,
                answer.how,
                answer.uri,
            )
            resolved.append(answer)
        return resolved

    async def _python_entry(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        distribution: DistributionRecord | None,
    ) -> Path | None:
        """The installed console script, for ``python`` packaging.

        Built once per extension and version; a development install
        may build it from the bundle directory, a distributed one only
        from the wheels it carries.
        """
        if manifest.python is None:
            return None
        if self._environments is None:
            raise InstallError(
                f"{manifest.id} is a Python package and this backend has nowhere "
                "to install one, so it cannot start here."
            )
        return await self._environments.ensure(manifest, bundle, development=distribution is None)

    def _service_url(
        self, manifest: ExtensionManifest, distribution: DistributionRecord | None
    ) -> str | None:
        """The service this extension belongs to, if it has one.

        From the distribution record — igni says which service shipped
        it — or, for a development install, the extension's own
        ``serviceUrl`` setting, because there is no record to ask.
        """
        if distribution is not None:
            return distribution.service_url
        if self._store is None:
            return None
        value = self._store.settings(manifest).get(SERVICE_URL_SETTING)
        text = str(value).strip() if value is not None else ""
        return text or None

    async def _process_grants(
        self, manifest: ExtensionManifest, service_url: str | None
    ) -> dict[str, str]:
        """How this extension's process reaches igni: its host and its service.

        Only for a process igni starts. One secret per start, shared by
        the two loopback endpoints, so one value names one process
        wherever it presents it: the host model's channel
        (``IGNI_HOST_URL``, ``IGNI_HOST_SECRET``) and, when the extension
        belongs to a service, the token endpoint for it
        (``IGNI_SERVICE_TOKEN_URL``, ``IGNI_SERVICE_TOKEN_SECRET``).
        """
        if not manifest.runs_here:
            return {}
        secret = secrets.token_urlsafe(32)
        env: dict[str, str] = {}
        if self._service_tokens is not None and service_url is not None:
            env.update(await self._service_tokens.grant(manifest.id, service_url, secret=secret))
        if self._host_channel is not None:
            env.update(await self._host_channel.grant(manifest.id, secret))
        return env

    def _revoke_token(self, extension_id: str) -> None:
        if self._service_tokens is not None:
            self._service_tokens.revoke(extension_id)
        if self._host_channel is not None:
            self._host_channel.revoke(extension_id)

    def _process_environment(
        self,
        literal: dict[str, str],
        permissions: Permissions,
        resolved: list[Resolved],
        token_env: dict[str, str],
    ) -> dict[str, str]:
        """A started process's environment, in precedence order.

        The manifest's own literals first, so nothing igni grants can
        be shadowed by a value the bundle shipped — an extension
        setting ``IGNI_WORKSPACE`` or ``IGNI_SERVICE_TOKEN_URL`` itself
        must not be how it gets one. Then the grants, then what the
        resolver found, then the token endpoint.
        """
        env = dict(literal)
        env.update(permissions.service_environment(self._project_dir))
        env.update(Connections(resolved).environment())
        env.update(token_env)
        return env

    def _service_config(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        permissions: Permissions,
        resolved: list[Resolved],
        entry_point: Path | None = None,
        token_env: dict[str, str] | None = None,
    ) -> MCPServerConfig:
        """How to reach this extension's service, within its grants."""
        name = server_name(manifest.id)

        if manifest.python is not None:
            if entry_point is None:
                raise InstallError(f"{manifest.id}: its package was not installed")
            return MCPServerConfig(
                name=name,
                type=MCPTransport.stdio,
                command=str(entry_point),
                args=[],
                env=self._process_environment({}, permissions, resolved, token_env or {}),
                cwd=str(bundle),
                source="extension",
            )

        if manifest.dev is not None:
            cwd = bundle if manifest.dev.cwd is None else bundle / manifest.dev.cwd
            return MCPServerConfig(
                name=name,
                type=MCPTransport.stdio,
                command=manifest.dev.command[0],
                args=list(manifest.dev.command[1:]),
                env=self._process_environment(
                    dict(manifest.dev.env), permissions, resolved, token_env or {}
                ),
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
            "starting container images is not implemented yet. Use 'python' "
            "to ship a package, 'endpoint' to register a service you run, or "
            "'dev' to run a command locally."
        )

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

        **The two sides speak different names.** ``get_tools``
        returns registered names (``mcp_ext:acme.scanner_list``) and
        the manifest declares the server's own (``list``, or
        ``acme.scanner.list``). This compared them directly, so
        nothing ever matched and every tool was refused — including
        the declared ones. It looked like working enforcement
        because the failure is in the safe direction, and the unit
        test agreed because it passed bare names, which is not what
        this hands it.
        """
        client = self._mcp.get_client(server)
        if client is None:
            return
        by_bare = {bare_tool_name(server, name): name for name in self._mcp.get_tools(server)}
        refused = permissions.tools.refused(by_bare)
        for tool in refused:
            self._mcp.set_tool_enabled(server, by_bare[tool], False)
        if refused:
            logger.info(
                "extension %s offered %d tools its manifest does not declare; not registering %s",
                manifest.id,
                len(refused),
                refused,
            )

    @staticmethod
    def _check_tool_audience(manifest: ExtensionManifest) -> None:
        """Refuse ``own-agents`` with no agents to be its own.

        The audience is "agents the carried plugin ships". With no
        plugin there are none, so every tool would be offered to
        nobody — approved at install and unreachable forever, the
        failure this contract keeps removing.
        """
        if manifest.permissions.toolAudience != "own-agents":
            return
        if not manifest.contributes.plugin:
            raise InstallError(
                f"{manifest.id} offers its tools to its own agents only "
                "(toolAudience: own-agents) and carries no plugin, so it "
                "has no agents of its own. Ship them with "
                "contributes.plugin, or use toolAudience: all-agents."
            )

    def _apply_tool_audience(self, manifest: ExtensionManifest) -> None:
        """Offer the service only to the agents its plugin ships.

        By directory, not by name: igni does not know which agents
        an extension has or what they are called, only where it put
        them.
        """
        if manifest.permissions.toolAudience != "own-agents":
            return
        if self._plugins_dir is None:
            # Nowhere to have put the agents, so nobody qualifies.
            # Restricting to a directory that does not exist is the
            # honest consequence rather than falling open.
            self._audience.restrict(server_name(manifest.id), Path("/nonexistent"))
            return
        self._audience.restrict(server_name(manifest.id), self._plugins_dir / manifest.id)

    def _install_plugin(self, manifest: ExtensionManifest, bundle: Path) -> None:
        """Put the plugin an extension carries where the loader looks.

        Agents, skills and hooks are not contribution types, because
        igni already delivers them through plugins — and a second
        path for the same thing is a second thing to keep in step.
        So an extension *wraps* one, and this copies it into the
        plugins directory under the extension's own id.

        Under the extension's id rather than the plugin's own name:
        uninstalling has to find it again, and a plugin free to call
        itself anything is a plugin that can be left behind, or can
        collide with one somebody installed by hand.

        Failures are logged, not raised. An extension whose service
        works and whose agents did not arrive is degraded; refusing
        the install would be worse, and the line says which it is.
        """
        name = manifest.contributes.plugin
        if not name:
            return
        if self._plugins_dir is None:
            logger.warning(
                "extension %s carries a plugin and this deployment has no "
                "plugins directory configured, so its agents, skills and "
                "hooks will not be loaded.",
                manifest.id,
            )
            return

        source = (bundle / name).resolve()
        # Inside the bundle, checked rather than trusted: the path
        # comes from a manifest, and ``../../..`` is a manifest
        # asking igni to copy somebody else's directory.
        if not source.is_relative_to(bundle.resolve()) or not source.is_dir():
            logger.error(
                "extension %s names a plugin directory that is not in its "
                "bundle: %r. Not installed.",
                manifest.id,
                name,
            )
            return
        if not (source / ".claude-plugin" / "plugin.json").is_file():
            logger.error(
                "extension %s names %r as a plugin and it has no "
                ".claude-plugin/plugin.json, so the loader would skip it "
                "silently. Not installed.",
                manifest.id,
                name,
            )
            return

        target = self._plugins_dir / manifest.id
        try:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
        except OSError:
            logger.exception("installing %s's plugin failed", manifest.id)
            return
        logger.info("extension %s installed its plugin at %s", manifest.id, target)

    def _remove_plugin(self, extension_id: str) -> None:
        """Take the carried plugin away with the extension.

        An agent left behind is worse than one that never arrived:
        the archivist with no Artifacts installed is an agent whose
        every tool call fails, offered in the same list as the ones
        that work.
        """
        if self._plugins_dir is None:
            return
        target = self._plugins_dir / extension_id
        if not target.is_dir():
            return
        try:
            shutil.rmtree(target)
        except OSError:  # pragma: no cover — best effort
            logger.exception("removing %s's plugin failed", extension_id)
            return
        logger.info("extension %s's plugin removed from %s", extension_id, target)

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
