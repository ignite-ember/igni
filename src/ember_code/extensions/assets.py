"""An extension's assets: the files it runs that are not Python.

A database, a JVM, a model. igni provides none of them, and a machine
with no network must still install the extension, so each one ships with
it — declared in ``manifest.json`` with the platform it is for, a plain
file name, its size, its SHA-256 and the ``source`` the service's build
fetched it from::

    "assets": [
      {"name": "neo4j", "platform": "any", "file": "neo4j-community-5.26.0-unix.tar.gz",
       "size": 158822080, "sha256": "…", "source": "https://…", "unpack": true},
      {"name": "jre", "platform": "macos-arm64", …}, …
    ]

The manifest is inside the signed bundle, so the bundle's signature
covers every asset through its digest. The bytes travel beside the
bundle: igni server holds them (``docs/service-identity.md`` §8.2), the
distribution sync downloads the ones for this machine's platform into
:class:`AssetCache`, and the installer checks each against its digest
again and lays it out read-only under ``<bundle>/assets/<name>/`` —
unpacked when declared so — telling the process where in
``IGNI_EXTENSION_ASSETS``.

A development install (``IGNI_DEV_EXTENSIONS``) has no igni server
behind it, so a missing asset is fetched from its ``source`` instead,
held to the same digest. A distributed one never reaches past igni.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform as host_platform
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ember_code.core.activity.http import busy_call

logger = logging.getLogger(__name__)

#: The largest single asset igni takes.
MAX_ASSET_BYTES = 1024 * 1024 * 1024

#: The most an unpacked asset may expand to.
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024

#: Suffixes igni can unpack.
UNPACKABLE = (".tar.gz", ".tgz", ".zip")

#: What an extension's process is told the directory of its assets is.
ASSETS_DIR_ENV = "IGNI_EXTENSION_ASSETS"

#: Inside a bundle: where its assets are laid out, one directory each.
BUNDLE_ASSETS = "assets"

_CHUNK = 1024 * 1024


class AssetError(Exception):
    """An asset is missing, is not the bytes its manifest pins, or will not unpack."""


class AssetPlatform(StrEnum):
    """Where an asset is for. ``any`` is every platform."""

    ANY = "any"
    MACOS_ARM64 = "macos-arm64"
    LINUX_X86_64 = "linux-x86_64"
    WINDOWS_X86_64 = "windows-x86_64"

    @classmethod
    def specific(cls) -> tuple[AssetPlatform, ...]:
        """The platforms igni runs on."""
        return (cls.MACOS_ARM64, cls.LINUX_X86_64, cls.WINDOWS_X86_64)

    @classmethod
    def current(cls) -> AssetPlatform | None:
        """This machine, or ``None`` when igni ships nothing for it."""
        machine = host_platform.machine().lower()
        if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
            return cls.MACOS_ARM64
        if sys.platform.startswith("linux") and machine in {"x86_64", "amd64"}:
            return cls.LINUX_X86_64
        if sys.platform == "win32" and machine in {"x86_64", "amd64"}:
            return cls.WINDOWS_X86_64
        return None


class ExtensionAsset(BaseModel):
    """One asset, as the manifest declares it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    platform: AssetPlatform
    #: A plain file name: no directory, nothing that climbs out.
    file: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")]
    size: Annotated[int, Field(gt=0, le=MAX_ASSET_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    #: Where the service's build fetched it. https only.
    source: Annotated[str, Field(pattern=r"^https://[^\s]+$", max_length=2000)]
    #: Unpacked into its directory on install.
    unpack: bool = False

    @model_validator(mode="after")
    def _unpackable(self) -> Self:
        if self.unpack and not self.file.endswith(UNPACKABLE):
            raise ValueError(f"{self.file}: only {', '.join(UNPACKABLE)} can be unpacked")
        return self


class ExtensionAssets:
    """Every asset a manifest declares, checked as a set."""

    @staticmethod
    def check(assets: list[ExtensionAsset]) -> list[ExtensionAsset]:
        """A name is one file for ``any`` platform, or one for each platform igni runs on."""
        by_name: dict[str, list[AssetPlatform]] = {}
        for asset in assets:
            by_name.setdefault(asset.name, []).append(asset.platform)
        for name, platforms in by_name.items():
            if platforms == [AssetPlatform.ANY]:
                continue
            if sorted(platforms) != sorted(AssetPlatform.specific()):
                raise ValueError(
                    f"asset {name!r} must be one file for 'any' platform, or one for each of "
                    f"{', '.join(AssetPlatform.specific())}; it is for {', '.join(platforms)}"
                )
        return assets

    @staticmethod
    def for_platform(
        assets: Iterable[ExtensionAsset], platform: AssetPlatform
    ) -> list[ExtensionAsset]:
        return [a for a in assets if a.platform in (AssetPlatform.ANY, platform)]


class FileDigest:
    """SHA-256 of a file, read in chunks."""

    @staticmethod
    def of(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def matches(cls, path: Path, asset: ExtensionAsset) -> bool:
        return path.is_file() and path.stat().st_size == asset.size and cls.of(path) == asset.sha256


class ReadOnlyTree:
    """Making a laid-out asset read-only, and removing one that is."""

    @staticmethod
    def seal(root: Path) -> None:
        """Nothing under ``root`` can be written; what could run still can."""
        for path in [root, *root.rglob("*")]:
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(stat.S_IMODE(mode) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    @staticmethod
    def remove(root: Path) -> None:
        """``rmtree`` that first gives back the write permission sealing took."""
        if not root.exists() and not root.is_symlink():
            return

        def writable_then_retry(function, path, _exc) -> None:  # type: ignore[no-untyped-def]
            parent = Path(path).parent
            for target in (parent, Path(path)):
                if target.exists() and not target.is_symlink():
                    target.chmod(stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR)
            function(path)

        shutil.rmtree(root, onexc=writable_then_retry)


class AssetCache:
    """Downloaded assets on this machine, one file per digest.

    Shared by every extension, so a new version that keeps its Neo4j is
    not downloaded again; pruned to what installed manifests declare.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path(self, asset: ExtensionAsset) -> Path:
        return self._root / asset.sha256

    def holds(self, asset: ExtensionAsset) -> bool:
        return FileDigest.matches(self.path(asset), asset)

    def partial(self) -> Path:
        """A new file to download into, beside where it will land."""
        self._root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".partial-", dir=self._root)
        os.close(fd)
        return Path(name)

    def adopt(self, partial: Path, asset: ExtensionAsset) -> Path:
        """Keep a downloaded file if it is the asset's bytes; refuse it otherwise."""
        try:
            if not FileDigest.matches(partial, asset):
                raise AssetError(
                    f"asset {asset.name} ({asset.platform}) is not the {asset.size} bytes "
                    f"with SHA-256 {asset.sha256} its manifest pins"
                )
            return Path(partial.replace(self.path(asset)))
        finally:
            partial.unlink(missing_ok=True)

    def prune(self, keep: set[str]) -> None:
        """Forget every asset no installed manifest declares."""
        if not self._root.is_dir():
            return
        for path in self._root.iterdir():
            if path.name not in keep:
                path.unlink(missing_ok=True)


class AssetSource:
    """Fetches an asset from the ``source`` its manifest names — development installs only."""

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout: float = 900.0
    ) -> None:
        self._transport = transport
        self._timeout = timeout

    def fetch(self, asset: ExtensionAsset, cache: AssetCache) -> Path:
        partial = cache.partial()
        try:
            received = 0
            with (
                busy_call("extension-assets"),
                httpx.Client(
                    transport=self._transport, timeout=self._timeout, follow_redirects=True
                ) as client,
                client.stream("GET", asset.source) as response,
                partial.open("wb") as out,
            ):
                if response.status_code != 200 or response.url.scheme != "https":
                    raise AssetError(
                        f"asset {asset.name} ({asset.platform}): {asset.source} answered "
                        f"{response.status_code} from {response.url}"
                    )
                for chunk in response.iter_bytes(_CHUNK):
                    received += len(chunk)
                    if received > asset.size:
                        raise AssetError(
                            f"asset {asset.name} ({asset.platform}): {asset.source} is larger "
                            f"than the {asset.size} bytes its manifest pins"
                        )
                    out.write(chunk)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise AssetError(
                f"asset {asset.name} ({asset.platform}): {asset.source} could not be fetched: {exc}"
            ) from exc
        except AssetError:
            partial.unlink(missing_ok=True)
            raise
        return cache.adopt(partial, asset)


class AssetLayout:
    """Laying a bundle's assets out for its process, checked and read-only."""

    MARKER = ".sha256"

    def __init__(self, cache: AssetCache) -> None:
        self._cache = cache

    @staticmethod
    def root(bundle: Path) -> Path:
        return bundle / BUNDLE_ASSETS

    def lay_out(self, bundle: Path, assets: Iterable[ExtensionAsset]) -> Path:
        """``<bundle>/assets/<name>/`` for each, from the cache. Returns ``<bundle>/assets``.

        Each is checked against its digest before it is used, every
        time it is laid out; one already laid out from the same digest
        is left as it is.
        """
        root = self.root(bundle)
        root.mkdir(exist_ok=True)
        for asset in assets:
            target = root / asset.name
            marker = target / self.MARKER
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == asset.sha256:
                continue
            source = self._cache.path(asset)
            if not self._cache.holds(asset):
                raise AssetError(
                    f"asset {asset.name} ({asset.platform}) is not on this machine with the "
                    f"SHA-256 its manifest pins ({asset.sha256})"
                )
            ReadOnlyTree.remove(target)
            staging = root / f".{asset.name}.incoming"
            ReadOnlyTree.remove(staging)
            staging.mkdir()
            try:
                if asset.unpack:
                    Unpacker.unpack(source, staging, asset.file)
                else:
                    shutil.copyfile(source, staging / asset.file)
                (staging / self.MARKER).write_text(asset.sha256 + "\n", encoding="utf-8")
                ReadOnlyTree.seal(staging)
                staging.rename(target)
            except BaseException:
                ReadOnlyTree.remove(staging)
                raise
            logger.info("laid out asset %s (%s) at %s", asset.name, asset.platform, target)
        return root


class Unpacker:
    """Unpacking a ``.tar.gz`` or ``.zip`` without letting it reach outside itself."""

    @classmethod
    def unpack(cls, archive: Path, into: Path, name: str) -> None:
        if name.endswith((".tar.gz", ".tgz")):
            cls._tar(archive, into)
        elif name.endswith(".zip"):
            cls._zip(archive, into)
        else:  # pragma: no cover — the manifest refuses it first
            raise AssetError(f"{name} is not an archive igni unpacks")

    @staticmethod
    def _tar(archive: Path, into: Path) -> None:
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED_BYTES:
                    raise AssetError(f"{archive.name} unpacks to more than igni allows")
                # `data` refuses absolute paths, `..`, links that point
                # outside and device files, and keeps executable bits.
                tar.extractall(into, members=members, filter="data")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise AssetError(f"{archive.name} could not be unpacked: {exc}") from exc

    @staticmethod
    def _zip(archive: Path, into: Path) -> None:
        resolved = into.resolve()
        try:
            with zipfile.ZipFile(archive) as zipped:
                infos = zipped.infolist()
                if sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
                    raise AssetError(f"{archive.name} unpacks to more than igni allows")
                for info in infos:
                    target = (into / info.filename).resolve()
                    if not target.is_relative_to(resolved):
                        raise AssetError(
                            f"{archive.name} has a path outside itself: {info.filename}"
                        )
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise AssetError(f"{archive.name} has a link: {info.filename}")
                zipped.extractall(into)
                for info in infos:
                    mode = (info.external_attr >> 16) & 0o777
                    if mode & 0o111 and not info.is_dir():
                        (into / info.filename).chmod(mode)
        except (zipfile.BadZipFile, OSError) as exc:
            raise AssetError(f"{archive.name} could not be unpacked: {exc}") from exc
