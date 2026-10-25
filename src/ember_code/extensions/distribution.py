"""Extensions arrive from their services, through igni server.

igni server's ``docs/service-identity.md`` §8. A service uploads its extension to
igni, signed with the key it registered; once a super admin approves
the service, igni lists the extension to every signed-in machine.
This is the machine's half: keep what is installed in step with that
list.

For every listed extension whose version or digest differs from what
is installed:

1. download ``GET /v1/extensions/{id}/bundle``;
2. check the archive's SHA-256 against the listing and the
   ``X-Igni-Bundle-Sha256`` header;
3. verify ``X-Igni-Bundle-Signature`` — a compact JWS — against the
   ``publisher_jwks`` the listing carries, and that its claim
   ``{extension, version, sha256}`` is this archive;
4. unpack it (no path outside itself, no links, no devices, bounded
   size) and check the manifest names the same id and version;
5. download each asset its manifest declares for this machine's
   platform that is not on this machine already —
   ``GET /v1/extensions/{id}/assets/{sha256}`` — and keep it only if it
   is the bytes the signed manifest pins (§8.2);
6. store it with a :class:`~ember_code.extensions.store.DistributionRecord`
   and hand it to the ordinary boot path — trust, profile, capability
   resolution — through :class:`ExtensionLifecycle`.

An extension igni distributed whose service is no longer listed is
stopped and removed. A **development install** of the same id is
left alone in both directions: a developer running their own build of
an extension is not overwritten by the released one, and its absence
from the listing is not a reason to delete it.

**Never in the way.** A sync runs in the background, at start-up and
on sign-in. Not signed in, no server configured, igni unreachable, a
bundle that does not verify — each is logged, and whatever is
installed keeps running. A failure here costs an update, never the
extension already working.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import logging
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ember_code.core.config.endpoint import is_configured
from ember_code.extensions.assets import (
    AssetCache,
    AssetError,
    AssetPlatform,
    ExtensionAsset,
    ExtensionAssets,
)
from ember_code.extensions.bundle_signature import BundleSignatureError, PublisherKeys
from ember_code.extensions.impact import Resolution
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.service_token import IgniSession
from ember_code.extensions.store import DistributionRecord, ExtensionStore, StoreError

logger = logging.getLogger(__name__)

#: igni server's limit on an upload, so the most a download can be.
MAX_ARCHIVE_BYTES = 400 * 1024 * 1024

#: The most an archive may unpack to. Wheels are already compressed;
#: this bounds a bomb.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024

SIGNATURE_HEADER = "X-Igni-Bundle-Signature"
SHA256_HEADER = "X-Igni-Bundle-Sha256"

#: How long one asset may take to arrive: a JVM or a database is
#: hundreds of megabytes.
ASSET_DOWNLOAD_TIMEOUT = 1800.0


class DistributionError(Exception):
    """One extension could not be brought up to date, and why."""


class DistributedExtension(BaseModel):
    """One row of ``GET /v1/extensions``."""

    model_config = ConfigDict(extra="ignore")

    extension_id: str
    version: str
    name: str = ""
    description: str = ""
    #: The service it belongs to — and the audience of its tokens.
    service_url: str
    sha256: str
    signature: str = ""
    publisher_jwks: dict[str, Any]
    published_at: datetime | None = None


class DistributionListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    extensions: list[DistributedExtension] = Field(default_factory=list)
    #: A super admin's decisions on collisions between the extensions
    #: listed — :mod:`ember_code.extensions.impact`.
    resolutions: list[Resolution] = Field(default_factory=list)


class DownloadedBundle(BaseModel):
    archive: bytes
    signature: str
    sha256: str = ""


class SyncReport(BaseModel):
    """What one sync did, for the log and for tests."""

    #: Why nothing was attempted, when nothing was.
    skipped: str | None = None
    installed: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    #: Development installs a listed extension did not replace.
    development: list[str] = Field(default_factory=list)
    #: Whether the decisions on collisions changed.
    resolutions_changed: bool = False
    failed: dict[str, str] = Field(default_factory=dict)


class ExtensionLifecycle(Protocol):
    """Starting and stopping an extension in the running backend."""

    async def activate(self, extension_id: str, bundle: Path) -> None: ...

    async def deactivate(self, extension_id: str) -> None: ...


class DistributionClient:
    """The two distribution calls, with the person's igni session."""

    LISTING_PATH = "/v1/extensions"

    def __init__(
        self,
        api_url: str,
        session: IgniSession,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
        download_timeout: float = 300.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {session.access_token}"}
        self._transport = transport
        self._timeout = timeout
        self._download_timeout = download_timeout

    async def listing(self) -> DistributionListing:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{self._api_url}{self.LISTING_PATH}", headers=self._headers
                )
        except httpx.HTTPError as exc:
            raise DistributionError(f"igni could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise DistributionError(f"igni refused the extension listing ({response.status_code})")
        try:
            return DistributionListing.model_validate_json(response.content)
        except ValidationError as exc:
            raise DistributionError(f"the extension listing is malformed: {exc}") from exc

    async def bundle(self, extension_id: str) -> DownloadedBundle:
        url = f"{self._api_url}{self.LISTING_PATH}/{extension_id}/bundle"
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._download_timeout, transport=self._transport
                ) as client,
                client.stream("GET", url, headers=self._headers) as response,
            ):
                if response.status_code != 200:
                    raise DistributionError(
                        f"igni refused the bundle download ({response.status_code})"
                    )
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > MAX_ARCHIVE_BYTES:
                        raise DistributionError("the bundle is larger than any igni accepts")
                return DownloadedBundle(
                    archive=bytes(received),
                    signature=response.headers.get(SIGNATURE_HEADER, ""),
                    sha256=response.headers.get(SHA256_HEADER, ""),
                )
        except httpx.HTTPError as exc:
            raise DistributionError(f"the bundle download failed: {exc}") from exc

    async def asset(self, extension_id: str, asset: ExtensionAsset, cache: AssetCache) -> Path:
        """Download one asset into ``cache``, held to its size and digest on the way."""
        url = f"{self._api_url}{self.LISTING_PATH}/{extension_id}/assets/{asset.sha256}"
        partial = cache.partial()
        try:
            async with (
                httpx.AsyncClient(
                    timeout=ASSET_DOWNLOAD_TIMEOUT, transport=self._transport
                ) as client,
                client.stream("GET", url, headers=self._headers) as response,
            ):
                if response.status_code != 200:
                    raise DistributionError(
                        f"igni refused asset {asset.name} ({asset.platform}): "
                        f"{response.status_code}"
                    )
                received = 0
                with partial.open("wb") as out:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > asset.size:
                            raise DistributionError(
                                f"asset {asset.name} ({asset.platform}) is larger than "
                                "its manifest pins"
                            )
                        out.write(chunk)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise DistributionError(f"asset {asset.name} could not be downloaded: {exc}") from exc
        except DistributionError:
            partial.unlink(missing_ok=True)
            raise
        try:
            return cache.adopt(partial, asset)
        except AssetError as exc:
            raise DistributionError(str(exc)) from exc


class BundleArchive:
    """A downloaded archive: its digest, and unpacking it safely."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.sha256 = hashlib.sha256(archive).hexdigest()

    def extract(self, target: Path) -> None:
        """Unpack into ``target``, refusing anything that escapes it.

        The ``data`` filter refuses absolute paths, ``..``, links that
        point outside and device files; the size check before it
        bounds what a small archive can expand to.
        """
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(self.archive), mode="r:gz") as tar:
                members = tar.getmembers()
                if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED_BYTES:
                    raise DistributionError("the bundle unpacks to more than igni allows")
                tar.extractall(target, members=members, filter="data")
        except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as exc:
            raise DistributionError(f"the bundle could not be unpacked: {exc}") from exc


class BundleVerifier:
    """Every check between a download and the store."""

    @staticmethod
    def verify(entry: DistributedExtension, downloaded: DownloadedBundle) -> BundleArchive:
        archive = BundleArchive(downloaded.archive)
        if downloaded.sha256 and downloaded.sha256.lower() != archive.sha256:
            raise DistributionError("the archive does not match the digest igni sent with it")
        if entry.sha256.lower() != archive.sha256:
            raise DistributionError("the archive is not the one the listing describes")
        if not downloaded.signature:
            raise DistributionError(f"the bundle came without {SIGNATURE_HEADER}")
        try:
            claim = PublisherKeys(entry.publisher_jwks).verify(downloaded.signature)
        except BundleSignatureError as exc:
            raise DistributionError(str(exc)) from exc
        if claim.sha256.lower() != archive.sha256:
            raise DistributionError("the signature is for different bytes than were downloaded")
        if (claim.extension, claim.version) != (entry.extension_id, entry.version):
            raise DistributionError(
                f"the signature is for {claim.extension} {claim.version}, and igni "
                f"listed {entry.extension_id} {entry.version}"
            )
        return archive


class ExtensionDistribution:
    """Keeps this machine's extensions in step with igni's listing."""

    def __init__(
        self,
        store: ExtensionStore,
        *,
        api_url: Callable[[], str | None],
        session: Callable[[], IgniSession | None],
        lifecycle: ExtensionLifecycle | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        on_resolutions: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            on_resolutions: Told when the decisions on collisions
                changed, after they are stored.
        """
        self._store = store
        self._api_url = api_url
        self._session = session
        self._lifecycle = lifecycle
        self._on_resolutions = on_resolutions
        self._transport = transport
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[SyncReport | None]] = set()

    def schedule(self) -> asyncio.Task[SyncReport | None]:
        """Sync in the background. Nothing waits for it.

        Two calls close together run one after the other rather than
        at once: a sign-in during the start-up sync must still see the
        listing for the person who just signed in.
        """
        task = asyncio.get_running_loop().create_task(self._sync_logged())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sync_logged(self) -> SyncReport | None:
        try:
            report = await self.sync()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a background sync must not take anything down
            logger.exception("extension distribution sync failed")
            return None
        if report.skipped:
            logger.info("extension distribution skipped: %s", report.skipped)
        elif report.installed or report.removed or report.failed:
            logger.info(
                "extension distribution: installed %s, removed %s, failed %s",
                report.installed,
                report.removed,
                report.failed,
            )
        return report

    async def sync(self) -> SyncReport:
        """One pass: list, update what differs, remove what is gone."""
        async with self._lock:
            session = self._session()
            if session is None:
                return SyncReport(skipped="nobody is signed in to igni")
            api_url = self._api_url()
            if not is_configured(api_url):
                return SyncReport(skipped="no igni server is configured")
            assert api_url is not None

            client = DistributionClient(api_url, session, transport=self._transport)
            try:
                listing = await client.listing()
            except DistributionError as exc:
                # Nothing is removed on a failed listing: "igni did not
                # answer" is not "the service is gone".
                logger.warning("extension listing failed, keeping what is installed: %s", exc)
                return SyncReport(skipped=str(exc))

            report = SyncReport()
            # Decisions first: an extension installed below starts
            # with them already applied.
            if self._store.record_resolutions(listing.resolutions):
                report.resolutions_changed = True
                if self._on_resolutions is not None:
                    self._on_resolutions()
            for entry in listing.extensions:
                await self._bring_up_to_date(client, entry, report)
            await self._remove_unlisted(
                {entry.extension_id for entry in listing.extensions}, report
            )
            self._store.prune_assets()
            return report

    async def _bring_up_to_date(
        self, client: DistributionClient, entry: DistributedExtension, report: SyncReport
    ) -> None:
        extension_id = entry.extension_id
        try:
            installed_at = self._store.path_for(extension_id)
        except StoreError as exc:
            report.failed[extension_id] = str(exc)
            return
        record = self._store.distribution_of(extension_id)

        if record is None and installed_at.is_dir():
            logger.info(
                "extension %s is installed for development; the version igni "
                "distributes (%s) is not replacing it",
                extension_id,
                entry.version,
            )
            report.development.append(extension_id)
            return

        if (
            record is not None
            and installed_at.is_dir()
            and (record.version, record.sha256.lower()) == (entry.version, entry.sha256.lower())
        ):
            if record.service_url != entry.service_url:
                # Same bundle, service moved: the token audience
                # changes, so the process is restarted with the new one.
                self._store.record_distribution(self._record(entry))
                await self._restart(extension_id, installed_at)
            report.unchanged.append(extension_id)
            return

        try:
            await self._install(client, entry)
        except DistributionError as exc:
            logger.warning(
                "extension %s %s was not installed: %s", extension_id, entry.version, exc
            )
            report.failed[extension_id] = str(exc)
            return
        report.installed.append(extension_id)

    async def _install(self, client: DistributionClient, entry: DistributedExtension) -> None:
        downloaded = await client.bundle(entry.extension_id)
        archive = BundleVerifier.verify(entry, downloaded)

        with tempfile.TemporaryDirectory(prefix="igni-ext-") as scratch:
            staging = Path(scratch) / "bundle"
            archive.extract(staging)
            try:
                manifest = self._store.read(staging)
            except StoreError as exc:
                raise DistributionError(str(exc)) from exc
            if (manifest.id, manifest.version) != (entry.extension_id, entry.version):
                raise DistributionError(
                    f"the bundle's manifest is {manifest.id} {manifest.version}, and "
                    f"its signature is for {entry.extension_id} {entry.version}"
                )
            if manifest.is_development_command:
                raise DistributionError(
                    f"{manifest.id} uses 'dev' packaging, a command out of its "
                    "manifest, which igni does not install from a service"
                )
            # Before the old version stops: a download that fails leaves
            # the one that works running.
            await self._fetch_assets(client, manifest)

            # Stopped before its files are replaced: a running process
            # whose environment is deleted under it fails in ways that
            # are no fun to read about.
            if self._lifecycle is not None:
                await self._lifecycle.deactivate(entry.extension_id)
            try:
                target = self._store.add(staging, replace=True, distribution=self._record(entry))
            except (StoreError, OSError) as exc:
                # The store keeps the old bundle when a replace fails,
                # so the version that worked is started again.
                previous = self._store.path_for(entry.extension_id)
                if self._lifecycle is not None and previous.is_dir():
                    await self._lifecycle.activate(entry.extension_id, previous)
                raise DistributionError(f"storing it failed: {exc}") from exc

        logger.info(
            "extension %s %s installed from %s",
            entry.extension_id,
            entry.version,
            entry.service_url,
        )
        if self._lifecycle is not None:
            await self._lifecycle.activate(entry.extension_id, target)

    async def _fetch_assets(self, client: DistributionClient, manifest: ExtensionManifest) -> None:
        """This machine's assets of ``manifest``, each one not here already."""
        if not manifest.assets:
            return
        platform = AssetPlatform.current()
        if platform is None:
            raise DistributionError(
                f"{manifest.id} ships assets for "
                f"{', '.join(sorted({a.platform for a in manifest.assets}))}, and this "
                "machine is none of the platforms igni runs on"
            )
        cache = self._store.asset_cache
        for asset in ExtensionAssets.for_platform(manifest.assets, platform):
            if cache.holds(asset):
                continue
            logger.info(
                "downloading asset %s (%s) of %s, %d bytes",
                asset.name,
                asset.platform,
                manifest.id,
                asset.size,
            )
            await client.asset(manifest.id, asset, cache)

    async def _restart(self, extension_id: str, bundle: Path) -> None:
        if self._lifecycle is None:
            return
        await self._lifecycle.deactivate(extension_id)
        await self._lifecycle.activate(extension_id, bundle)

    async def _remove_unlisted(self, listed: set[str], report: SyncReport) -> None:
        for record in self._store.distributed():
            if record.extension_id in listed:
                continue
            if self._lifecycle is not None:
                await self._lifecycle.deactivate(record.extension_id)
            self._store.remove(record.extension_id)
            logger.info(
                "extension %s removed: its service (%s) is no longer listed",
                record.extension_id,
                record.service_url,
            )
            report.removed.append(record.extension_id)

    @staticmethod
    def _record(entry: DistributedExtension) -> DistributionRecord:
        return DistributionRecord(
            extension_id=entry.extension_id,
            version=entry.version,
            sha256=entry.sha256.lower(),
            service_url=entry.service_url,
            published_at=entry.published_at,
        )
