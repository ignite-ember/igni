"""What one extension's sandbox lets through, decided once, OS-independent.

:class:`SandboxPlan` is the grant list the platform profiles
(:mod:`.seatbelt`, :mod:`.bubblewrap`) are written from:

* **files it may read** — its bundle (the install dir, its assets
  included), its Python environment and the interpreter that
  environment was made from, its own data directory, its run's temp
  directory, and the workspace when ``permissions.workspace`` grants it
  (a ``workspacePaths`` glob as its directory prefix). Outside the home
  directories the system is readable, because a process needs its
  libraries, certificates and tools; every home directory is not, apart
  from the paths above.
* **files it may write** — its data directory, its run's temp directory
  (also its ``HOME``), and the workspace only when the grant is
  ``write``.
* **hosts it may reach** — through the proxy only: its own service and
  its ``permissions.egress``, narrowed to the network's own hosts when
  air-gapped.
* **loopback ports** igni serves it on — the host channel and the token
  endpoint — which a process in a network namespace needs bridged in.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ember_code.core.network_policy import NetworkPolicy
from ember_code.extensions.manifest import ExtensionManifest, Workspace
from ember_code.extensions.sandbox.proxy import HostAllowList

#: Glob characters: a ``workspacePaths`` entry is granted up to the first.
_GLOB = frozenset("*?[")


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _both(path: Path) -> list[Path]:
    """A path as written and as it resolves; a profile must name the real one."""
    out = [path]
    real = _real(path)
    if real != path:
        out.append(real)
    return out


def _dedupe(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


@dataclass(slots=True)
class SandboxPlan:
    """One extension process's grants, ready to write a profile from."""

    extension_id: str
    read: list[Path] = field(default_factory=list)
    write: list[Path] = field(default_factory=list)
    hosts: HostAllowList = field(default_factory=HostAllowList)
    #: Hosts the manifest grants that an air gap took away, for the log.
    withheld: list[str] = field(default_factory=list)
    #: Its service, when it is plain http outside development and so not
    #: granted: said once at start-up, since the proxy's refusal cannot
    #: tell "not its service" from "its service, over http".
    plain_http_service: str | None = None
    #: igni's own loopback ports the process is told about.
    loopback_ports: list[int] = field(default_factory=list)
    #: igni's own loopback ports it was not granted (macOS denies them;
    #: Linux has its own loopback, where they do not exist).
    denied_ports: list[int] = field(default_factory=list)
    #: Its run's private directory: igni's, not the process's.
    run_dir: Path | None = None

    @property
    def temp_dir(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir / "tmp"

    @property
    def home_dir(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir / "tmp" / "home"

    @classmethod
    def build(
        cls,
        manifest: ExtensionManifest,
        *,
        command: str,
        env: dict[str, str],
        cwd: Path | None,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
        service_url: str | None,
        development: bool,
        network: NetworkPolicy,
        run_dir: Path,
    ) -> SandboxPlan:
        plan = cls(extension_id=manifest.id, run_dir=run_dir)
        plan._grant_files(manifest, command, env, cwd, bundle, data_dir, project_dir)
        plan._grant_hosts(manifest, service_url, development, network)
        plan._note_loopback(env, service_url)
        return plan

    # ── files ──────────────────────────────────────────────────────

    def _grant_files(
        self,
        manifest: ExtensionManifest,
        command: str,
        env: dict[str, str],
        cwd: Path | None,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
    ) -> None:
        read: list[Path] = [*_both(bundle)]
        if cwd is not None:
            read += _both(cwd)
        read += self.runtime_paths(command, env)
        write: list[Path] = [*_both(self.temp_dir)]
        if data_dir is not None:
            write += _both(data_dir)
        mode = manifest.permissions.workspace
        if project_dir is not None and mode is not Workspace.NONE:
            granted = self.workspace_paths(project_dir, manifest.permissions.workspacePaths)
            read += granted
            if mode is Workspace.WRITE:
                write += granted
        # What may be written may be read.
        self.read = _dedupe(read + write)
        self.write = _dedupe(write)

    @staticmethod
    def workspace_paths(project_dir: Path, globs: list[str]) -> list[Path]:
        """The workspace, or each glob's directory prefix within it.

        A sandbox grants paths, not patterns, so ``src/**/*.py`` is
        granted as ``src``: wider than the glob, never wider than the
        workspace. A glob that climbs out (``..``) is dropped.
        """
        if not globs:
            return _both(project_dir)
        root = _real(project_dir)
        out: list[Path] = []
        for glob in globs:
            parts: list[str] = []
            for part in Path(glob).parts:
                if _GLOB & set(part):
                    break
                parts.append(part)
            candidate = _real(root.joinpath(*parts)) if parts else root
            if candidate == root or root in candidate.parents:
                out.append(candidate)
        return _dedupe(out)

    @staticmethod
    def runtime_paths(command: str, env: dict[str, str]) -> list[Path]:
        """What the command needs to read to start: its interpreter's tree.

        For a console script or interpreter inside a virtual environment,
        the environment and the Python it was made from (``pyvenv.cfg``'s
        ``home``). Otherwise the directory above the executable's
        ``bin``. Only what lies under a home directory matters — the
        rest of the system is readable anyway.
        """
        path = Path(command)
        if not path.is_absolute():
            found = shutil.which(command, path=env.get("PATH") or os.environ.get("PATH"))
            if found is None:
                return []
            path = Path(found)
        out: list[Path] = []
        for exe in dict.fromkeys([path, _real(path)]):
            prefix = exe.parent.parent
            cfg = prefix / "pyvenv.cfg"
            if cfg.is_file():
                out += _both(prefix)
                home = _pyvenv_home(cfg)
                if home is not None:
                    out += _both(home.parent)
            else:
                out += _both(prefix)
        return _dedupe(out)

    # ── network ────────────────────────────────────────────────────

    def _grant_hosts(
        self,
        manifest: ExtensionManifest,
        service_url: str | None,
        development: bool,
        network: NetworkPolicy,
    ) -> None:
        entries: list[str] = []
        if service_url:
            parts = urlsplit(service_url.strip())
            try:
                port = parts.port
            except ValueError:
                port = None
            host = parts.hostname
            if host and (parts.scheme == "https" or (parts.scheme == "http" and development)):
                default = 443 if parts.scheme == "https" else 80
                bracketed = f"[{host}]" if ":" in host else host
                entries.append(f"{bracketed}:{port or default}")
            elif host and parts.scheme == "http":
                self.plain_http_service = service_url.strip()
        entries += list(manifest.permissions.egress)
        for entry in entries:
            pair = HostAllowList.parse(entry)
            if pair is None:
                continue
            host, port = pair
            if network.air_gapped and not network.is_inside(host):
                self.withheld.append(entry)
                continue
            self.hosts.add(host, port)

    def _note_loopback(self, env: dict[str, str], service_url: str | None) -> None:
        ports: list[int] = []
        for value in [*env.values(), service_url or ""]:
            if "://" not in value:
                continue
            parts = urlsplit(value)
            try:
                port = parts.port
            except ValueError:
                continue
            if parts.hostname and NetworkPolicy.is_loopback(parts.hostname) and port:
                ports.append(port)
        self.loopback_ports = list(dict.fromkeys(ports))


def _pyvenv_home(cfg: Path) -> Path | None:
    try:
        for line in cfg.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                return Path(value.strip())
    except OSError:
        return None
    return None


__all__ = ["SandboxPlan"]
