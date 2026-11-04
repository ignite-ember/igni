"""The files an extension runs that are not Python: its assets.

An extension that runs a database, a JVM or a model ships them itself, so
a machine with no network installs it (igni provides none of them). Each
one is declared in `manifest.json`:

    "assets": [
      {"name": "neo4j", "platform": "any",
       "file": "neo4j-community-5.26.0-unix.tar.gz",
       "size": 158822080, "sha256": "…",
       "source": "https://dist.neo4j.org/neo4j-community-5.26.0-unix.tar.gz",
       "unpack": true},
      {"name": "jre", "platform": "macos-arm64", …},
      {"name": "jre", "platform": "linux-x86_64", …},
      {"name": "jre", "platform": "windows-x86_64", …}
    ]

A name is either one file for every platform (`any`) or one per platform
igni runs on, all three. `source` is where the bundle builder fetches it,
at build time, on a machine with a network; `size` and `sha256` pin what
it must be. The manifest is inside the signed bundle, so the signature
covers every asset through its digest.

Assets travel **beside** the bundle archive, not inside it: the builder
writes them to `<bundle>.assets/<sha256>`, the service uploads each one
igni does not already hold (signed, §8.2), and a machine downloads only
its own platform's. A new version that keeps its Neo4j moves only the
archive.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from igni_service.errors import BundleError

#: The largest single asset igni takes.
MAX_ASSET_BYTES = 1024 * 1024 * 1024

#: Suffixes igni can unpack on install.
UNPACKABLE = (".tar.gz", ".tgz", ".zip")

_CHUNK = 1024 * 1024


class AssetPlatform(StrEnum):
    """Where an asset is for. `any` is every platform."""

    ANY = "any"
    MACOS_ARM64 = "macos-arm64"
    LINUX_X86_64 = "linux-x86_64"
    WINDOWS_X86_64 = "windows-x86_64"

    @classmethod
    def specific(cls) -> tuple[AssetPlatform, ...]:
        """The platforms igni runs on."""
        return (cls.MACOS_ARM64, cls.LINUX_X86_64, cls.WINDOWS_X86_64)


class BundleAsset(BaseModel):
    """One declared asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    platform: AssetPlatform
    #: A plain file name: no directory, nothing that climbs out.
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
    size: int = Field(gt=0, le=MAX_ASSET_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Where the bundle builder fetches it. https only.
    source: str = Field(pattern=r"^https://[^\s]+$", max_length=2000)
    #: Unpacked by igni on install (a `.tar.gz`, `.tgz` or `.zip`).
    unpack: bool = False

    @model_validator(mode="after")
    def _unpackable(self) -> Self:
        if self.unpack and not self.file.endswith(UNPACKABLE):
            raise ValueError(f"{self.file}: only {', '.join(UNPACKABLE)} can be unpacked")
        return self


class BundleAssets(BaseModel):
    """Every asset a manifest declares, checked as a set."""

    model_config = ConfigDict(frozen=True)

    assets: list[BundleAsset] = Field(default_factory=list)

    @field_validator("assets")
    @classmethod
    def _each_name_covers_every_platform(cls, assets: list[BundleAsset]) -> list[BundleAsset]:
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

    def for_platform(self, platform: AssetPlatform) -> list[BundleAsset]:
        """What a machine on `platform` needs."""
        return [a for a in self.assets if a.platform in (AssetPlatform.ANY, platform)]

    def digests(self) -> set[str]:
        return {asset.sha256 for asset in self.assets}


class BundleLayout:
    """Where the builder puts what it builds."""

    @staticmethod
    def assets_dir(bundle: Path) -> Path:
        """`extension-bundle.tar.gz` → `extension-bundle.assets`, beside it."""
        name = re.sub(r"(\.tar\.gz|\.tgz)$", "", bundle.name)
        return bundle.with_name(f"{name}.assets")


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
    def matches(cls, path: Path, asset: BundleAsset) -> bool:
        return path.is_file() and path.stat().st_size == asset.size and cls.of(path) == asset.sha256


class AssetFetcher:
    """Fetches declared assets to `<dir>/<sha256>`, each held to its size and digest."""

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout: float = 600.0
    ) -> None:
        self._transport = transport
        self._timeout = timeout

    def fetch(self, asset: BundleAsset, into: Path) -> Path:
        into.mkdir(parents=True, exist_ok=True)
        target = into / asset.sha256
        if FileDigest.matches(target, asset):
            return target
        fd, partial_name = tempfile.mkstemp(prefix=".partial-", dir=into)
        partial = Path(partial_name)
        try:
            digest = hashlib.sha256()
            received = 0
            with (
                os.fdopen(fd, "wb") as out,
                httpx.Client(
                    transport=self._transport, timeout=self._timeout, follow_redirects=True
                ) as client,
                client.stream("GET", asset.source) as response,
            ):
                if response.status_code != 200:
                    raise BundleError(
                        f"asset {asset.name} ({asset.platform}): {asset.source} answered "
                        f"{response.status_code}"
                    )
                if response.url.scheme != "https":
                    raise BundleError(
                        f"asset {asset.name} ({asset.platform}): {asset.source} redirected "
                        f"to {response.url}, which is not https"
                    )
                for chunk in response.iter_bytes(_CHUNK):
                    received += len(chunk)
                    if received > asset.size:
                        raise BundleError(
                            f"asset {asset.name} ({asset.platform}): {asset.source} is larger "
                            f"than the {asset.size} bytes declared"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if received != asset.size or digest.hexdigest() != asset.sha256:
                raise BundleError(
                    f"asset {asset.name} ({asset.platform}): {asset.source} is {received} bytes "
                    f"with SHA-256 {digest.hexdigest()}; the manifest pins {asset.size} bytes "
                    f"and {asset.sha256}"
                )
            partial.replace(target)
            return target
        except httpx.HTTPError as exc:
            raise BundleError(
                f"asset {asset.name} ({asset.platform}): {asset.source} could not be fetched: {exc}"
            ) from exc
        finally:
            partial.unlink(missing_ok=True)
