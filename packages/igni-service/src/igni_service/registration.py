"""Registering with igni server, and shipping the extension.

On every start:

1. load this deployment's key, or make one (`SigningKey`);
2. `POST /v1/clients` with RFC 7591 metadata and the public key — the
   same key again returns the same registration and its state, so
   registering on every start is how a restart finds its client id;
3. log the key's thumbprint, loudly: a super admin compares it with the
   one igni's portal shows before approving;
4. ask igni which extension bundle it holds for this client and, when it
   holds different bytes (or none), upload this one, signed.

All of it retried with backoff in the background, so a service whose
igni is briefly down still starts and serves.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import tarfile
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from igni_service.crypto import BundleClaim, Jwk
from igni_service.errors import BundleError, IgniError, IgniUnavailable
from igni_service.server import IgniServer
from igni_service.signing import SigningKey
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)

#: The header igni reads a bundle's signature from.
SIGNATURE_HEADER = "X-Igni-Bundle-Signature"


class ClientJwks(BaseModel):
    keys: list[Jwk]


class ClientMetadata(BaseModel):
    """The RFC 7591 body a service registers with."""

    client_name: str
    client_uri: str
    kind: str = "service"
    redirect_uris: list[str] = Field(default_factory=list)
    token_endpoint_auth_method: str = "private_key_jwt"
    jwks: ClientJwks


class Registration(BaseModel):
    """What igni answers a registration with (RFC 7591 §3.2.1, plus its state)."""

    client_id: str
    client_uri: str = ""
    state: str = ""
    thumbprint: str = ""


class PublishedExtension(BaseModel):
    """What igni holds for this client's extension."""

    extension_id: str
    version: str
    sha256: str


class ExtensionRefused(IgniError):
    """igni read the bundle and refused it. The same bytes will be refused again."""


class ExtensionBundle:
    """A bundle archive, read for its manifest and never unpacked."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.sha256 = hashlib.sha256(archive).hexdigest()
        self.extension_id, self.version = self._identity(archive)

    @classmethod
    def at(cls, path: str | Path) -> ExtensionBundle | None:
        """The bundle at `path`, or `None` when there is none."""
        where = Path(path)
        if not str(path) or not where.is_file():
            return None
        return cls(where.read_bytes())

    @property
    def claim(self) -> BundleClaim:
        return BundleClaim(extension=self.extension_id, version=self.version, sha256=self.sha256)

    @staticmethod
    def _identity(archive: bytes) -> tuple[str, str]:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if member.isfile() and member.name.removeprefix("./") == "manifest.json":
                        extracted = tar.extractfile(member)
                        if extracted is None:
                            break
                        manifest = json.loads(extracted.read())
                        return str(manifest["id"]), str(manifest["version"])
        except (tarfile.TarError, OSError, EOFError, ValueError, KeyError) as exc:
            raise BundleError(f"the extension bundle is unreadable: {exc}") from exc
        raise BundleError("the extension bundle has no manifest.json at its root")


class ServiceRegistration:
    """This service's registration with igni, and its extension there."""

    #: Backoff while igni cannot be reached at start, in seconds.
    RETRY_MIN = 5.0
    RETRY_MAX = 300.0
    #: An upload may be large; it gets longer than an ordinary call.
    UPLOAD_TIMEOUT = 120.0

    def __init__(
        self,
        igni: IgniServer,
        key: SigningKey,
        *,
        service_name: str,
        kind: str = "service",
        redirect_uris: list[str] | None = None,
        bundle_path: str = "",
        retry_min: float | None = None,
        retry_max: float | None = None,
    ) -> None:
        self._igni = igni
        self._key = key
        self._service_name = service_name
        self._kind = kind
        self._redirect_uris = list(redirect_uris or [])
        self._bundle_path = bundle_path
        self._retry_min = self.RETRY_MIN if retry_min is None else retry_min
        self._retry_max = self.RETRY_MAX if retry_max is None else retry_max
        self._registration: Registration | None = None
        self._lock = asyncio.Lock()
        self.registered = asyncio.Event()

    @property
    def current(self) -> Registration | None:
        """The registration igni last answered with, or `None` before one."""
        return self._registration

    async def metadata(self) -> ClientMetadata:
        pair = await self._key.pair()
        return ClientMetadata(
            client_name=self._service_name,
            client_uri=self._igni.own_url,
            kind=self._kind,
            redirect_uris=self._redirect_uris,
            jwks=ClientJwks(keys=[pair.public_jwk()]),
        )

    async def register(self) -> Registration:
        """Register, or find this key's registration again. Raises `IgniUnavailable`."""
        async with self._lock:
            body = (await self.metadata()).model_dump(exclude_none=True)
            try:
                response = await self._igni.http.post(self._igni.url(IgniPaths.CLIENTS), json=body)
            except httpx.HTTPError as exc:
                raise IgniUnavailable(
                    f"could not reach igni server to register: {type(exc).__name__}"
                ) from exc
            if response.status_code not in (200, 201):
                raise IgniUnavailable(
                    f"igni server refused the registration: {response.status_code} {response.text}"
                )
            try:
                self._registration = Registration.model_validate(response.json())
            except (ValidationError, ValueError) as exc:
                raise IgniUnavailable(
                    "igni server answered the registration with something else"
                ) from exc
            self.registered.set()
            return self._registration

    async def client_id(self) -> str:
        """This service's client id at igni, registering first if need be."""
        registration = self._registration or await self.register()
        return registration.client_id

    async def published(self) -> PublishedExtension | None:
        """What igni holds for this client's extension, or `None`."""
        client_id = await self.client_id()
        try:
            response = await self._igni.http.get(self._igni.url(IgniPaths.extension(client_id)))
        except httpx.HTTPError as exc:
            raise IgniUnavailable(
                f"could not ask igni server about the extension: {type(exc).__name__}"
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise IgniUnavailable(
                f"igni server would not say which extension it holds: {response.status_code}"
            )
        try:
            return PublishedExtension.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise IgniUnavailable("igni server answered with something else") from exc

    async def ship(self, bundle: ExtensionBundle) -> bool:
        """Upload `bundle` unless igni already holds these exact bytes. True when uploaded."""
        held = await self.published()
        if held is not None and held.sha256 == bundle.sha256:
            logger.info(
                "igni: already holds extension %s %s (%s)",
                bundle.extension_id,
                bundle.version,
                bundle.sha256,
            )
            return False
        client_id = await self.client_id()
        signature = await self._key.sign_bundle(bundle.claim)
        try:
            response = await self._igni.http.put(
                self._igni.url(IgniPaths.extension(client_id)),
                content=bundle.archive,
                headers={"Content-Type": "application/gzip", SIGNATURE_HEADER: signature},
                timeout=max(self._igni.timeout, self.UPLOAD_TIMEOUT),
            )
        except httpx.HTTPError as exc:
            raise IgniUnavailable(f"could not upload the extension: {type(exc).__name__}") from exc
        if 400 <= response.status_code < 500:
            raise ExtensionRefused(
                f"igni server refused extension {bundle.extension_id} {bundle.version}: "
                f"{response.status_code} {response.text}"
            )
        if response.status_code != 200:
            raise IgniUnavailable(
                f"igni server could not take the extension: {response.status_code} {response.text}"
            )
        logger.info(
            "igni: uploaded extension %s %s (%s)",
            bundle.extension_id,
            bundle.version,
            bundle.sha256,
        )
        return True

    async def start(self) -> Registration:
        """Register, say the thumbprint, ship the extension. Raises `IgniUnavailable`."""
        registration = await self.register()
        logger.warning(
            "\n"
            "  ============================================================\n"
            "  igni service identity\n"
            "    client id:   %s\n"
            "    state:       %s\n"
            "    URL:         %s\n"
            "    thumbprint:  %s\n"
            "  A super admin approves this service in igni's portal under\n"
            "  Services, after checking the thumbprint there is this one.\n"
            "  ============================================================",
            registration.client_id,
            registration.state,
            registration.client_uri or self._igni.own_url,
            await self._key.thumbprint(),
        )
        try:
            bundle = ExtensionBundle.at(self._bundle_path)
        except BundleError as exc:
            # Not retried: the file will be just as unreadable in five
            # minutes. The registration stands; the image is broken.
            logger.error("igni: %s; nothing uploaded", exc)
            return registration
        if bundle is None:
            if self._bundle_path:
                logger.warning(
                    "igni: no extension bundle at %s; nothing uploaded", self._bundle_path
                )
            return registration
        try:
            await self.ship(bundle)
        except ExtensionRefused as exc:
            # Not retried either: igni read these bytes and said no.
            logger.error("igni: %s", exc)
        return registration

    async def keep_trying(self, stop: asyncio.Event | None = None) -> Registration | None:
        """`start`, retried with backoff until it succeeds or `stop` is set."""
        stop = stop or asyncio.Event()
        delay = self._retry_min
        while not stop.is_set():
            try:
                return await self.start()
            except IgniUnavailable as exc:
                logger.error("igni: registration failed (%s); retrying in %.0fs", exc, delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, self._retry_max)
        return None
