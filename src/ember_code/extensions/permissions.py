"""Turning declared permissions into enforced ones.

The manifest modelled six permissions and the schema validated all
six. Exactly one — ``permissions.rpc`` — was enforced by anything.
``egress``, ``secrets``, ``workspace``, ``tools`` and ``signature``
were read by nobody, so the extension boundary was a comment.

This module is where the other four are decided. ``signature`` has
left the manifest: provenance is decided by where a bundle came from —
igni server's distribution, verified in
:mod:`ember_code.extensions.distribution` — rather than by scope.

A service token is never a ``secrets`` grant. The host fetches it for
the extension and serves it on loopback
(:mod:`ember_code.extensions.service_token`), so the extension never
holds the person's igni session.

## What enforcement means here, honestly

Each of these is enforced at the point igni controls, and that point
differs:

* **egress** — the UI's CSP is written from the manifest and the
  extension's own service, so a page cannot open a connection to a
  host nobody declared, and reaches each one over https/wss only
  (plain http/ws to this machine, in development). The *service*
  is a separate process and confining its network needs an OS
  sandbox this does not have; what is enforced there is narrower and
  stated as such on :meth:`Egress.check_endpoint`.
* **tools** — igni registers an extension's MCP tools with the
  agent, so igni can decline to register one the manifest did not
  declare. This is complete.
* **workspace** — igni decides what it *tells* a service about the
  project. A service that goes looking anyway is not stopped by
  this, and that is also an OS-sandbox problem.
* **secrets** — a name not declared is never resolved. What a
  declared one resolves to is a value in the service's environment,
  not a handle; see :class:`Secrets`.

Writing the limits down is the point. A permission that is partly
enforced and documented as total is the failure this module exists to
end.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ember_code.core.mcp.hints import ToolHints
from ember_code.extensions.manifest import ExtensionManifest, Workspace

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """Something the manifest did not ask for, refused."""


#: Hosts that are this machine. Plain ``http``/``ws`` to one of them is
#: allowed in development — a service on a laptop has no certificate —
#: and nowhere else.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ServiceOrigin:
    """An extension's own service, as an origin a page may reach.

    The service is the one that shipped the extension (the
    distribution record's ``service_url``, or a development install's
    ``serviceUrl`` setting) — the same one its process holds a token
    for. A page reaches it over ``https`` and ``wss``; plain ``http``
    and ``ws`` only to this machine in development. Anything else is
    not an origin a page is given: :meth:`parse` answers ``None`` and
    the page has no service.

    The origin, not the URL: a CSP source with a path matches that
    path only, and a service's API is not under its home page.
    """

    def __init__(self, scheme: str, host: str, port: int | None) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port

    @classmethod
    def parse(cls, url: str | None, *, development: bool = False) -> ServiceOrigin | None:
        """The origin of ``url``, or ``None`` when a page may not reach it."""
        text = (url or "").strip()
        if not text:
            return None
        parsed = urlparse(text)
        try:
            port = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if not host or parsed.scheme not in ("https", "http"):
            return None
        if parsed.scheme == "http" and not (development and host in LOCAL_HOSTS):
            logger.warning(
                "service %s is plain http and not this machine in development; "
                "a page is not given a connection to it",
                text,
            )
            return None
        return cls(parsed.scheme, host, port)

    @property
    def secure(self) -> bool:
        return self.scheme == "https"

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.port is not None else host

    @property
    def url(self) -> str:
        """``https://host[:port]``: what ``bridge.service.url`` is."""
        return f"{self.scheme}://{self.authority}"

    def csp_sources(self) -> list[str]:
        """Its HTTP origin and its WebSocket origin, nothing wider."""
        socket = "wss" if self.secure else "ws"
        return [self.url, f"{socket}://{self.authority}"]


class Egress:
    """Which hosts this extension may reach.

    Two lists, one rule. The extension's **own service** is reachable
    because it is the extension's; it needs no declaration. Every
    **other** host is declared in ``permissions.egress``, shown to the
    administrator who approves the service, and enforced here. Both
    over ``https``/``wss`` only — plain ``http``/``ws`` only to this
    machine, in development. Exact origins: the manifest schema allows
    a host and a port and nothing else, so there is no wildcard to
    widen.

    The service token is never part of this. It goes to the
    extension's own service only (``bridge.service``, ``ext.service``);
    an egress host is reachable, not trusted.
    """

    def __init__(self, manifest: ExtensionManifest, *, development: bool = False) -> None:
        self._id = manifest.id
        self._hosts = tuple(manifest.permissions.egress)
        self._development = development

    @property
    def hosts(self) -> tuple[str, ...]:
        return self._hosts

    def csp_connect_src(self, service: ServiceOrigin | None = None) -> str:
        """The ``connect-src`` for this extension's pages.

        ``'self'`` always, because a page has to reach its own origin
        for its own assets. Then the extension's own service, when it
        has one, and every declared host — each as ``https://`` and
        ``wss://`` and never scheme-less: a bare host in a CSP matches
        any scheme including ``ws:``, and an allowlist should be as
        wide as it reads. A localhost egress host in development also
        gets ``http://`` and ``ws://``.
        """
        sources = ["'self'"]
        if service is not None:
            sources.extend(service.csp_sources())
        for host in self._hosts:
            sources.extend(self._host_sources(host))
        # A declared host that is also the service is listed once.
        return " ".join(dict.fromkeys(sources))

    def _host_sources(self, host: str) -> list[str]:
        sources = [f"https://{host}", f"wss://{host}"]
        if self._development and self._is_local(host):
            sources += [f"http://{host}", f"ws://{host}"]
        return sources

    @staticmethod
    def _is_local(host: str) -> bool:
        return host.rsplit(":", 1)[0] in LOCAL_HOSTS

    def check_endpoint(self, url: str) -> None:
        """Refuse an endpoint whose host was not declared, or is not https.

        Narrow and real: igni makes this connection itself, so igni
        can refuse it. It is not network confinement of the
        extension's own process — nothing here stops a service from
        opening its own sockets, and claiming otherwise would be the
        kind of overstatement this module exists to remove.
        """
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            raise PermissionDenied(f"{self._id}: endpoint {url!r} has no host")
        if host not in self._hosts and parsed.netloc not in self._hosts:
            raise PermissionDenied(
                f"{self._id}: its endpoint is at {host!r}, which is not in "
                f"permissions.egress ({list(self._hosts) or 'empty'}). Declare it "
                "and reinstall — an undeclared destination is the one failure "
                "that costs a customer their accreditation."
            )
        if parsed.scheme != "https" and not (self._development and host in LOCAL_HOSTS):
            raise PermissionDenied(
                f"{self._id}: its endpoint {url!r} is not https. Plain http is "
                "allowed only to this machine, in development."
            )


class Tools:
    """Which MCP tools may be registered with the agent.

    Complete enforcement, because registration is igni's. An
    extension's service may expose whatever it likes; what reaches
    the model is this list.
    """

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._id = manifest.id
        self._hints = {tool.name: tool.hints for tool in manifest.permissions.tools}
        self._declared = set(self._hints)

    @property
    def declared(self) -> set[str]:
        return set(self._declared)

    def hints(self, tool: str) -> ToolHints | None:
        """What the manifest says ``tool`` is — by the server's name or the namespaced one."""
        if tool in self._hints:
            return self._hints[tool]
        return self._hints.get(f"{self._id}.{tool}")

    def allowed(self, tool: str) -> bool:
        """Whether ``tool`` may reach the agent.

        Matched against the declared names and against the same names
        with the extension's id stripped, because a server's own tool
        is called ``list`` while a manifest namespaces it as
        ``acme.scanner.list`` — the manifest is read by an
        administrator, so it is the one that should carry the
        namespace.
        """
        if tool in self._declared:
            return True
        prefix = f"{self._id}."
        return any(
            declared.startswith(prefix) and declared[len(prefix) :] == tool
            for declared in self._declared
        )

    def refused(self, offered: Iterable[str]) -> list[str]:
        """Of what the service offers, what will not be registered."""
        return sorted(tool for tool in offered if not self.allowed(tool))


class WorkspaceAccess:
    """What igni tells a service about the project."""

    #: Environment the service reads to find what it was granted.
    DIR = "IGNI_WORKSPACE"
    MODE = "IGNI_WORKSPACE_MODE"
    PATHS = "IGNI_WORKSPACE_PATHS"

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._id = manifest.id
        self._mode = manifest.permissions.workspace
        self._globs = tuple(manifest.permissions.workspacePaths)

    @property
    def granted(self) -> bool:
        return self._mode is not Workspace.NONE

    def environment(self, project_dir: Path | None) -> dict[str, str]:
        """What to put in the service's environment.

        Empty when the grant is ``none``, which is the default. An
        extension that was never granted the workspace is not told
        where it is — that is the enforcement available at this
        boundary, and it is the difference between an extension
        having to be told and having to be stopped.
        """
        if not self.granted or project_dir is None:
            return {}
        env = {self.DIR: str(project_dir), self.MODE: self._mode.value}
        if self._globs:
            env[self.PATHS] = ":".join(self._globs)
        return env

    def permits(self, relative: str, *, write: bool = False) -> bool:
        """Whether a path inside the project is within the grant.

        For igni's own use when it acts on a service's behalf. A
        service reading the filesystem directly is not subject to
        this; confining a process is an OS sandbox and this is not
        one.
        """
        if not self.granted:
            return False
        if write and self._mode is not Workspace.WRITE:
            return False
        if not self._globs:
            return True
        return any(fnmatch.fnmatch(relative, glob) for glob in self._globs)


@dataclass(frozen=True, slots=True)
class SecretGrant:
    """One named secret an extension declared."""

    name: str
    value: str


class Secrets:
    """Named grants, resolved by the deployment.

    **This passes values, not handles.** The manifest's description
    once said "your code receives a handle, never a value", which
    described a broker with an exchange step that does not exist: an
    extension's service is an MCP server igni calls, so there is no
    channel for it to call back on. The honest statement is the one
    in ``EXTENSIBILITY.md``: a declared secret is resolved by the
    deployment and reaches the service in its environment, and the
    handle exchange is on the list of things not built.

    What *is* enforced, and matters: a name the manifest did not
    declare is never resolved, and the resolver is the deployment's
    rather than anything the bundle supplies.
    """

    def __init__(
        self,
        manifest: ExtensionManifest,
        resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._id = manifest.id
        self._declared = tuple(manifest.permissions.secrets)
        self._resolver = resolver

    @property
    def declared(self) -> tuple[str, ...]:
        return self._declared

    def resolve(self) -> list[SecretGrant]:
        """Every declared secret the deployment could supply.

        A declared name the deployment has no value for is a refusal
        rather than an empty string: an extension that silently gets
        `""` for its API token fails somewhere far away, usually as
        an authentication error against somebody else's service.
        """
        if not self._declared:
            return []
        if self._resolver is None:
            raise PermissionDenied(
                f"{self._id} declares secrets {list(self._declared)} and this "
                "deployment has no secret source configured."
            )
        grants: list[SecretGrant] = []
        missing: list[str] = []
        for name in self._declared:
            value = self._resolver(name)
            if value is None:
                missing.append(name)
                continue
            grants.append(SecretGrant(name=name, value=value))
        if missing:
            raise PermissionDenied(
                f"{self._id} declares secrets this deployment cannot supply: "
                f"{missing}. Add them to the secret source, or remove them from "
                "the manifest."
            )
        return grants

    def environment(self) -> dict[str, str]:
        """Resolved grants, as the service's environment reads them.

        Named ``IGNI_SECRET_<NAME>`` with the name upper-cased and
        non-alphanumerics replaced, so a manifest key like
        ``ledger-token`` is readable as ``IGNI_SECRET_LEDGER_TOKEN``.
        """
        return {
            f"IGNI_SECRET_{self._env_name(grant.name)}": grant.value for grant in self.resolve()
        }

    @staticmethod
    def _env_name(name: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in name).upper()


class Permissions:
    """Every scope check for one extension, in one place."""

    def __init__(
        self,
        manifest: ExtensionManifest,
        *,
        secret_resolver: Callable[[str], str | None] | None = None,
        development: bool = False,
    ) -> None:
        """
        Args:
            development: ``IGNI_DEV_EXTENSIONS`` is set, which is what
                lets plain ``http``/``ws`` reach this machine.
        """
        self.manifest = manifest
        self.egress = Egress(manifest, development=development)
        self.tools = Tools(manifest)
        self.workspace = WorkspaceAccess(manifest)
        self.secrets = Secrets(manifest, secret_resolver)

    def service_environment(self, project_dir: Path | None) -> dict[str, str]:
        """Everything the service is told, from its grants alone.

        Order matters only in that secrets are resolved last: a
        deployment that cannot supply one refuses the install, and
        doing that after the cheap checks keeps the failure about the
        secret rather than about whatever came after it.
        """
        env = dict(self.workspace.environment(project_dir))
        env.update(self.secrets.environment())
        return env
