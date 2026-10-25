"""What igni tells this process in its environment.

igni starts an extension's process with variables it alone sets — an
extension's manifest cannot shadow them:

* `IGNI_WORKSPACE`, `IGNI_WORKSPACE_MODE`, `IGNI_WORKSPACE_PATHS` — the
  project, when the workspace permission was granted;
* `IGNI_SECRET_<NAME>` — each declared secret;
* `IGNI_SERVICE_URL`, `IGNI_SERVICE_TOKEN_URL`, `IGNI_SERVICE_TOKEN_SECRET`
  — the extension's own service and where to ask for a token to it;
* `IGNI_EXTENSION_DATA` — a directory that is this extension's alone,
  kept across reinstalls and updates of its bundle, for whatever it
  stores on this machine;
* `IGNI_EXTENSION_ASSETS` — where the assets its manifest declares for
  this platform are laid out, read-only, one directory each
  (`<assets>/<name>/`), checked against the digests the signed manifest
  pins: the database, the JVM, the model it ships so it installs with
  no network;
* `IGNI_HOST_URL`, `IGNI_HOST_SECRET` — the host's object model
  (`ext.host`), on a loopback socket, and this process's secret for it —
  the same secret as the token endpoint's.

`ExtensionEnvironment` reads them once, typed. Nothing here is a setting
an author chooses; it is what this process was granted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from igni_extension.errors import NotConfigured


class WorkspaceMode(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class Workspace(BaseModel):
    """The project this process may read (or write), when it was granted one."""

    model_config = ConfigDict(frozen=True)

    root: Path | None = None
    mode: WorkspaceMode = WorkspaceMode.NONE
    #: Globs within the project the grant is limited to; empty means all of it.
    paths: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.root is not None and self.mode is not WorkspaceMode.NONE

    @property
    def writable(self) -> bool:
        return self.granted and self.mode is WorkspaceMode.WRITE


class Secrets(BaseModel):
    """Every declared secret the deployment supplied, by the manifest's name."""

    model_config = ConfigDict(frozen=True)

    by_env_name: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def env_name(name: str) -> str:
        """`ledger-token` → `LEDGER_TOKEN`, the way igni writes it."""
        return "".join(ch if ch.isalnum() else "_" for ch in name).upper()

    def get(self, name: str) -> str | None:
        return self.by_env_name.get(self.env_name(name))


class ServiceEndpoint(BaseModel):
    """The extension's own service, and the host's token endpoint for it."""

    model_config = ConfigDict(frozen=True)

    url: str = ""
    token_url: str = ""
    token_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)


class HostEndpoint(BaseModel):
    """Where the igni host's object model is: `IGNI_HOST_URL`, `IGNI_HOST_SECRET`."""

    model_config = ConfigDict(frozen=True)

    url: str = ""
    secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.secret)


class Assets(BaseModel):
    """What the extension ships for this platform: `ext.assets.path("neo4j")`."""

    model_config = ConfigDict(frozen=True)

    #: `IGNI_EXTENSION_ASSETS`; None outside igni, or with nothing declared.
    root: Path | None = None

    def path(self, name: str) -> Path:
        """`<assets>/<name>/`: the unpacked archive, or the directory its one file is in.

        Raises `NotConfigured` when igni laid out no asset of that name —
        the manifest does not declare it, or this is not running in igni.
        """
        if self.root is None:
            raise NotConfigured(
                "igni laid out no assets for this process: the manifest declares none, "
                "or it is not running in igni"
            )
        where = self.root / name
        if not where.is_dir():
            raise NotConfigured(f"no asset named {name!r} was laid out at {self.root}")
        return where

    def file(self, name: str, file: str) -> Path:
        """One file of an asset that is not unpacked: `<assets>/<name>/<file>`."""
        where = self.path(name) / file
        if not where.is_file():
            raise NotConfigured(f"asset {name!r} has no file {file!r}")
        return where


class ExtensionEnvironment(BaseModel):
    """What igni granted this process, read from its environment."""

    model_config = ConfigDict(frozen=True)

    SECRET: ClassVar[str] = "IGNI_SECRET_"

    workspace: Workspace = Field(default_factory=Workspace)
    #: This extension's own directory on this machine; None outside igni.
    data_dir: Path | None = None
    #: What it ships for this platform, laid out by igni.
    assets: Assets = Field(default_factory=Assets)
    secrets: Secrets = Field(default_factory=Secrets)
    service: ServiceEndpoint = Field(default_factory=ServiceEndpoint)
    host: HostEndpoint = Field(default_factory=HostEndpoint)

    @classmethod
    def read(cls, environ: Mapping[str, str] | None = None) -> ExtensionEnvironment:
        env = dict(os.environ if environ is None else environ)
        secrets = Secrets(
            by_env_name={
                key.removeprefix(cls.SECRET): value
                for key, value in env.items()
                if key.startswith(cls.SECRET)
            }
        )
        data_dir = env.get("IGNI_EXTENSION_DATA", "").strip()
        assets = env.get("IGNI_EXTENSION_ASSETS", "").strip()
        return cls(
            workspace=cls._workspace(env),
            data_dir=Path(data_dir) if data_dir else None,
            assets=Assets(root=Path(assets) if assets else None),
            secrets=secrets,
            service=ServiceEndpoint(
                url=env.get("IGNI_SERVICE_URL", "").strip().rstrip("/"),
                token_url=env.get("IGNI_SERVICE_TOKEN_URL", "").strip(),
                token_secret=env.get("IGNI_SERVICE_TOKEN_SECRET", "").strip(),
            ),
            host=HostEndpoint(
                url=env.get("IGNI_HOST_URL", "").strip(),
                secret=env.get("IGNI_HOST_SECRET", "").strip(),
            ),
        )

    @staticmethod
    def _workspace(env: Mapping[str, str]) -> Workspace:
        root = env.get("IGNI_WORKSPACE", "").strip()
        if not root:
            return Workspace()
        try:
            mode = WorkspaceMode(env.get("IGNI_WORKSPACE_MODE", "read").strip() or "read")
        except ValueError:
            mode = WorkspaceMode.READ
        paths = tuple(p for p in env.get("IGNI_WORKSPACE_PATHS", "").split(":") if p)
        return Workspace(root=Path(root), mode=mode, paths=paths)
