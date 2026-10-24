"""`IgniService`: everything a service needs from igni, behind one object.

igni = IgniService.from_settings(IgniSettings())
app = FastAPI(lifespan=igni.lifespan)
app.include_router(igni.router)

@igni.on_erase
async def erase(subject: Subject) -> None: ...

@app.get("/things")
async def things(person: Person = Depends(igni.person)): ...

await igni.host(person.org_id).notify(Notification(...))
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from igni_service.clock import Clock
from igni_service.directory import DirectoryClient
from igni_service.errors import ConfigurationError, IgniUnavailable
from igni_service.host import Host
from igni_service.jwks import IgniKeys
from igni_service.keystore import KeyCipher, KeyStore
from igni_service.notifications import Notifier
from igni_service.registration import ServiceRegistration
from igni_service.server import IgniServer
from igni_service.service_tokens import ServiceTokens
from igni_service.settings import IgniSettings
from igni_service.signing import SigningKey
from igni_service.sql_keystore import SqlKeyStore
from igni_service.subjects import (
    EraseHandler,
    ExportHandler,
    SubjectHandlers,
    SubjectRoutes,
)
from igni_service.tokens import IgniCall, Person, TokenRefused, TokenVerifier
from igni_service.web import WebSignIn
from igni_service.web_sessions import MemoryWebSessionStore, SqlWebSessionStore, WebSessionStore

logger = logging.getLogger(__name__)

#: Lifts the bearer off a request. `auto_error=False` so a missing header
#: is refused with the same 401 as a bad one.
_BEARER = HTTPBearer(auto_error=False)

#: Said for every refusal. Telling "expired" from "wrong audience" from
#: "unknown key" would tell whoever holds the token which door it failed at.
_REFUSAL = "Not authenticated"


class IgniService:
    """A service's place in an igni deployment."""

    def __init__(
        self,
        settings: IgniSettings,
        *,
        key_store: KeyStore,
        web_store: WebSessionStore | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        retry_min: float | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("IGNI_SERVER_URL", settings.igni_server_url),
                ("PUBLIC_BASE_URL", settings.public_base_url),
                ("SERVICE_NAME", settings.service_name),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigurationError(f"igni: {', '.join(missing)} must be set")
        try:
            self.server = IgniServer(
                base_url=settings.igni_server_url,
                own_url=settings.public_base_url,
                timeout=settings.timeout,
                http=http,
            )
        except ValueError as exc:
            raise ConfigurationError(f"igni: PUBLIC_BASE_URL: {exc}") from exc
        self.settings = settings
        self.clock = clock or Clock()
        self.key = SigningKey(key_store, clock=self.clock)
        self.keys = IgniKeys(self.server, clock=self.clock)
        self.verifier = TokenVerifier(self.server, self.keys, clock=self.clock)
        self.registration = ServiceRegistration(
            self.server,
            self.key,
            service_name=settings.service_name,
            kind=settings.client_kind,
            redirect_uris=list(
                dict.fromkeys(
                    [WebSignIn.redirect_uri_for(self.server.own_url), *settings.redirect_uris]
                )
            ),
            bundle_path=settings.extension_bundle_path,
            retry_min=retry_min,
        )
        self.tokens = ServiceTokens(self.server, self.registration, self.key, clock=self.clock)
        self.directory = DirectoryClient(self.tokens, clock=self.clock)
        self.notifier = Notifier(self.tokens)
        self.web = WebSignIn(
            self.server,
            self.registration,
            self.key,
            self.verifier,
            web_store or MemoryWebSessionStore(clock=self.clock),
            clock=self.clock,
        )
        self._handlers = SubjectHandlers()
        self._router: APIRouter | None = None
        self._owned_engine: AsyncEngine | None = None

    @classmethod
    def from_settings(
        cls,
        settings: IgniSettings,
        *,
        key_store: KeyStore | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> IgniService:
        """A service from its settings, keeping its key and its people's
        sessions in `IGNI_KEY_DATABASE_URL`.

        Pass `key_store` to keep the key somewhere else — a `SqlKeyStore` on
        the service's own engine, usually — and then construct `IgniService`
        directly to pass a `web_store` too.
        """
        web_store: WebSessionStore | None = None
        engine: AsyncEngine | None = None
        if key_store is None:
            if not settings.key_database_url:
                raise ConfigurationError(
                    "igni: set IGNI_KEY_DATABASE_URL, or pass a key_store, so this service "
                    "can keep its key"
                )
            engine = create_async_engine(settings.key_database_url)
            cipher = KeyCipher(settings.key_encryption_key)
            key_store = SqlKeyStore(engine, cipher)
            web_store = SqlWebSessionStore(engine, cipher, clock=clock)
        service = cls(settings, key_store=key_store, web_store=web_store, http=http, clock=clock)
        service._owned_engine = engine
        return service

    # ── The FastAPI surface ─────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """Register with igni in the background for as long as the app runs.

        Usable as `FastAPI(lifespan=igni.lifespan)`, or entered inside the
        service's own lifespan (`async with igni.lifespan(app): ...`).
        """
        stop = asyncio.Event()
        registering = asyncio.create_task(self.registration.keep_trying(stop))
        try:
            yield
        finally:
            stop.set()
            registering.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await registering
            await self.aclose()

    async def aclose(self) -> None:
        """Close what this object opened: its HTTP client, and its database engine."""
        await self.server.aclose()
        if self._owned_engine is not None:
            await self._owned_engine.dispose()
            self._owned_engine = None

    @property
    def router(self) -> APIRouter:
        """The routes igni calls: erasure and export of one person."""
        if self._router is None:
            self._router = SubjectRoutes(self._handlers, self.directory, self.igni_call).router()
        return self._router

    def on_erase(self, handler: EraseHandler) -> EraseHandler:
        """Register what erases one person's data in one organisation."""
        return self._handlers.on_erase(handler)

    def on_export(self, handler: ExportHandler) -> ExportHandler:
        """Register what lists one person's data in one organisation."""
        return self._handlers.on_export(handler)

    async def person(
        self, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]
    ) -> Person:
        """FastAPI dependency: the person a request's token names.

        401 for a missing or refused token; 503 when igni's keys cannot be
        had to check it — nothing is wrong with the token, and signing in
        again would not help.
        """
        token = self._bare(credentials)
        try:
            return await self.verifier.person(token)
        except TokenRefused:
            raise self._refused() from None
        except IgniUnavailable as exc:
            raise self._cannot_check(exc) from exc

    async def igni_call(
        self, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]
    ) -> IgniCall:
        """FastAPI dependency: igni itself, for the organisation its token names."""
        token = self._bare(credentials)
        try:
            return await self.verifier.igni_call(token)
        except TokenRefused:
            raise self._refused() from None
        except IgniUnavailable as exc:
            raise self._cannot_check(exc) from exc

    # ── Talking to igni ─────────────────────────────────────────────

    def host(self, org_id: str) -> Host:
        """igni for one organisation: `notify`, and its `directory`."""
        return Host(org_id, notifier=self.notifier, directory=self.directory)

    async def thumbprint(self) -> str:
        """The key's RFC 7638 thumbprint: what a super admin compares."""
        return await self.key.thumbprint()

    @staticmethod
    def _bare(credentials: HTTPAuthorizationCredentials | None) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise IgniService._refused()
        return credentials.credentials

    @staticmethod
    def _refused() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REFUSAL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _cannot_check(exc: Exception) -> HTTPException:
        logger.error("igni: cannot check a token: %s", exc)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="igni server cannot be reached to check this token",
        )
