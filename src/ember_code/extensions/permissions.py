"""Turning declared permissions into enforced ones.

The manifest modelled six permissions and the schema validated all
six. Exactly one — ``permissions.rpc`` — was enforced by anything.
``egress``, ``secrets``, ``workspace``, ``tools`` and ``signature``
were read by nobody, so the extension boundary was a comment.

This module is where the other four are decided. ``signature`` lives
in :mod:`ember_code.extensions.signature` because it is a different
kind of check — provenance rather than scope.

## What enforcement means here, honestly

Each of these is enforced at the point igni controls, and that point
differs:

* **egress** — the UI's CSP is written from the manifest, so a page
  cannot open a connection to a host nobody declared. The *service*
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

from ember_code.extensions.events import EVENT_SINK_TOOL
from ember_code.extensions.manifest import ExtensionManifest, Workspace

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """Something the manifest did not ask for, refused."""


class Egress:
    """Which hosts this extension may reach."""

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._id = manifest.id
        self._hosts = tuple(manifest.permissions.egress)

    @property
    def hosts(self) -> tuple[str, ...]:
        return self._hosts

    def csp_connect_src(self) -> str:
        """The ``connect-src`` for this extension's pages.

        ``'self'`` always, because a page has to reach its own origin
        for its own assets. Everything else is what the manifest
        declared, scheme-less so it is written as ``https://host`` —
        a bare host in a CSP matches any scheme including ``ws:``,
        and an allowlist that quietly permits a websocket to a host
        declared for an API call is wider than it reads.
        """
        if not self._hosts:
            return "'self'"
        sources = " ".join(f"https://{host}" for host in self._hosts)
        return f"'self' {sources}"

    def check_endpoint(self, url: str) -> None:
        """Refuse an endpoint whose host was not declared.

        Narrow and real: igni makes this connection itself, so igni
        can refuse it. It is not network confinement of the
        extension's own process — nothing here stops a service from
        opening its own sockets, and claiming otherwise would be the
        kind of overstatement this module exists to remove.
        """
        host = urlparse(url).hostname
        if host is None:
            raise PermissionDenied(f"{self._id}: endpoint {url!r} has no host")
        if host not in self._hosts:
            raise PermissionDenied(
                f"{self._id}: its endpoint is at {host!r}, which is not in "
                f"permissions.egress ({list(self._hosts) or 'empty'}). Declare it "
                "and reinstall — an undeclared destination is the one failure "
                "that costs a customer their accreditation."
            )


class Tools:
    """Which MCP tools may be registered with the agent.

    Complete enforcement, because registration is igni's. An
    extension's service may expose whatever it likes; what reaches
    the model is this list.
    """

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._id = manifest.id
        self._declared = set(manifest.permissions.tools)

    @property
    def declared(self) -> set[str]:
        return set(self._declared)

    def allowed(self, tool: str) -> bool:
        """Whether ``tool`` may reach the agent.

        Matched against the declared names and against the same names
        with the extension's id stripped, because a server's own tool
        is called ``list`` while a manifest namespaces it as
        ``acme.scanner.list`` — the manifest is read by an
        administrator, so it is the one that should carry the
        namespace.

        One name is never allowed however it is declared:
        ``igni_on_event`` is how igni tells a service that something
        happened, and a model able to call it could tell an
        extension that HEAD moved when it had not. A control channel
        the agent can drive is not a control channel.
        """
        if tool == EVENT_SINK_TOOL:
            return False
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
    ) -> None:
        self.manifest = manifest
        self.egress = Egress(manifest)
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
