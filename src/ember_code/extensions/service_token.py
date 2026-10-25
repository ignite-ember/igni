"""An extension's token for its own service, fetched by the host.

igni server's ``docs/service-identity.md`` §9. An extension talks to the service it
belongs to — an extension to the service that shipped it — and that
service accepts a token igni issued for **its** URL. The extension
must never hold the person's igni session: whoever holds that can act
as them everywhere. So the host trades the session for a ten-minute
token for that one service (RFC 8693 token exchange, §3) and hands the
extension only the result.

**How the extension asks.** It is started with three variables:

* ``IGNI_SERVICE_URL`` — its service, from the distribution record or,
  for a development install, its own ``serviceUrl`` setting;
* ``IGNI_SERVICE_TOKEN_URL`` — a loopback URL on this host;
* ``IGNI_SERVICE_TOKEN_SECRET`` — a random secret for this process only.

``GET`` that URL with ``Authorization: Bearer <secret>`` and the answer
is ``{"access_token", "expires_in", "service_url"}``; ``401`` for a
wrong secret, ``503`` when nobody is signed in to igni.

**The secret is the only thing that names the extension.** One
listener serves every extension, and the secret decides which
extension is asking and therefore which service's token it gets — an
extension cannot ask for another's, because the audience is not a
parameter. Each process gets a fresh secret, revoked when it stops.

**A dedicated listener, not the extension's UI origin.** The origin in
:mod:`ember_code.extensions.origin` is where the extension's *page*
lives, inside a browser; a token endpoint on it would be one script
away from that page, and an extension with no pages has no origin at
all. Loopback only, a Host check against DNS rebinding, ``GET`` only,
one request per connection — the same discipline as the origin, and
its reply type.

**The page asks through the host, not here.** An extension's page
(``bridge.service``) has no secret and must not reach this listener. It
asks igni's window over the bridge, the window asks the backend with
the frame's extension as ``caller`` (``extension_service_token``), and
:class:`PageServiceTokens` answers from the same exchange and cache —
for the service recorded on that extension's own UI origin, the one its
CSP admits. The audience is again not a parameter.

**Cached, renewed shortly before expiry.** A token lives ten minutes;
the host keeps it and fetches a new one in the last minute, so an
extension asking on every request costs igni one exchange per ten
minutes. The cache is keyed by the session as well as the service, so
signing in as somebody else never serves the last person's token.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ValidationError

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.endpoint import is_configured
from ember_code.extensions.origin import (
    HEAD_TIMEOUT,
    LOOPBACK,
    MAX_HEAD_BYTES,
    ExtensionOrigins,
    HttpReply,
)
from ember_code.extensions.principal import Principal

logger = logging.getLogger(__name__)

#: The variables an extension process is started with.
ENV_SERVICE_URL = "IGNI_SERVICE_URL"
ENV_TOKEN_URL = "IGNI_SERVICE_TOKEN_URL"
ENV_TOKEN_SECRET = "IGNI_SERVICE_TOKEN_SECRET"

#: RFC 8693, as igni server's token endpoint names them.
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

#: Fetch a new token when the cached one has less than this left.
RENEW_BEFORE_SECONDS = 60.0

_JSON = "application/json; charset=utf-8"


class NotSignedIn(Exception):
    """Nobody is signed in to igni, so there is nothing to exchange."""


class TokenExchangeFailed(Exception):
    """igni did not issue a token, and why."""


class IgniSession(BaseModel):
    """The person's igni session, as the host holds it."""

    access_token: str
    #: The organisation the session is pinned to, sent along so the
    #: exchange does not depend on igni reading it out of the token.
    org_id: str | None = None


class CredentialsSession:
    """The session on disk, read fresh at every ask.

    Fresh because sign-in and sign-out rewrite the file, and a token
    endpoint that remembered the session would keep answering for
    somebody who had signed out.
    """

    def __init__(self, credentials_file: str | None = None) -> None:
        self._credentials_file = credentials_file

    def __call__(self) -> IgniSession | None:
        credentials = CloudCredentials(self._credentials_file)
        token = credentials.access_token
        if not token:
            return None
        return IgniSession(access_token=token, org_id=credentials.org_id)


class IssuedToken(BaseModel):
    """What igni's token endpoint answers, the parts read here."""

    access_token: str
    expires_in: int


class CachedToken(BaseModel):
    access_token: str
    service_url: str
    #: On :attr:`ServiceTokenExchange.clock`'s scale.
    expires_at: float


class ServiceTokenExchange:
    """Trades the igni session for service tokens, and keeps them."""

    TOKEN_PATH = "/v1/oauth/token"

    def __init__(
        self,
        *,
        api_url: Callable[[], str | None],
        session: Callable[[], IgniSession | None],
        renew_before: float = RENEW_BEFORE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_url = api_url
        self._session = session
        self._renew_before = renew_before
        self.clock = clock
        self._transport = transport
        self._timeout = timeout
        self._cache: dict[tuple[str, str], CachedToken] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def token_for(self, service_url: str) -> CachedToken:
        """A token for ``service_url``, from the cache while it is fresh.

        Raises:
            NotSignedIn: no igni session, or no igni server configured.
            TokenExchangeFailed: igni refused, or could not be reached.
        """
        session = self._session()
        if session is None:
            # Signed out: nothing cached may outlive that.
            self._cache.clear()
            raise NotSignedIn("nobody is signed in to igni")
        api_url = self._api_url()
        if not is_configured(api_url):
            raise NotSignedIn("no igni server is configured")
        assert api_url is not None

        key = (service_url, hashlib.sha256(session.access_token.encode()).hexdigest())
        fresh = self._fresh(key)
        if fresh is not None:
            return fresh
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Somebody else may have fetched it while we waited.
            fresh = self._fresh(key)
            if fresh is not None:
                return fresh
            issued = await self._exchange(api_url.rstrip("/"), session, service_url)
            token = CachedToken(
                access_token=issued.access_token,
                service_url=service_url,
                expires_at=self.clock() + issued.expires_in,
            )
            # One session at a time: entries for any other are stale.
            self._cache = {k: v for k, v in self._cache.items() if k[1] == key[1]}
            self._cache[key] = token
            return token

    def remaining(self, token: CachedToken) -> int:
        """Whole seconds ``token`` has left."""
        return max(int(token.expires_at - self.clock()), 0)

    def forget(self) -> None:
        self._cache.clear()

    def _fresh(self, key: tuple[str, str]) -> CachedToken | None:
        cached = self._cache.get(key)
        if cached is None or cached.expires_at - self.clock() <= self._renew_before:
            return None
        return cached

    async def _exchange(self, api_url: str, session: IgniSession, service_url: str) -> IssuedToken:
        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": session.access_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "audience": service_url,
        }
        if session.org_id:
            form["organisation"] = session.org_id
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(f"{api_url}{self.TOKEN_PATH}", data=form)
        except httpx.HTTPError as exc:
            raise TokenExchangeFailed(f"igni could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise TokenExchangeFailed(
                f"igni refused the exchange ({response.status_code}): {response.text[:300]}"
            )
        try:
            return IssuedToken.model_validate_json(response.content)
        except ValidationError as exc:
            raise TokenExchangeFailed(
                f"igni answered something that is not a token: {exc}"
            ) from exc


class ServiceGrant(BaseModel):
    """One extension process's right to ask, and what it may ask for."""

    extension_id: str
    service_url: str
    secret: str


class TokenReply(BaseModel):
    access_token: str
    expires_in: int
    service_url: str


class ErrorReply(BaseModel):
    error: str


class NoService(Exception):
    """This extension has no service its page may reach."""


class PageServiceTokens:
    """An extension page's token for its own service (``bridge.service``).

    The audience is the service recorded on the extension's UI origin —
    the one :class:`~ember_code.extensions.origin.ExtensionOrigin` wrote
    into the page's ``connect-src`` — and is looked up by the caller's
    id, which igni's window sets from the frame it hosts and the frame
    cannot choose. So a page gets a token for its own service and for
    nothing else: not another extension's, and not an egress host's.

    One exchange and one cache with the process's tokens: the page and
    the process of one extension cost igni one exchange per ten minutes.
    """

    def __init__(self, *, origins: ExtensionOrigins, exchange: ServiceTokenExchange) -> None:
        self._origins = origins
        self._exchange = exchange

    async def for_page(self, extension_id: str) -> TokenReply:
        """A token for ``extension_id``'s own service.

        Raises:
            NoService: it has no UI origin, or no service a page may reach.
            NotSignedIn: no igni session.
            TokenExchangeFailed: igni refused, or could not be reached.
        """
        service_url = self._origins.service_url_for(extension_id)
        if not service_url:
            raise NoService(f"{extension_id} has no service its page may reach")
        token = await self._exchange.token_for(service_url)
        return TokenReply(
            access_token=token.access_token,
            expires_in=self._exchange.remaining(token),
            service_url=service_url,
        )


class ServiceTokenEndpoint:
    """The loopback listener extensions fetch their tokens from."""

    PATH = "/v1/service-token"

    def __init__(
        self,
        exchange: ServiceTokenExchange,
        *,
        host: str = LOOPBACK,
        principal: Callable[[], Principal | None] | None = None,
    ) -> None:
        self._exchange = exchange
        self._host = host
        self._principal = principal
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._grants: dict[str, ServiceGrant] = {}
        self._start_lock = asyncio.Lock()

    @property
    def url(self) -> str:
        if not self._port:
            raise RuntimeError("the service-token endpoint is not listening; await start()")
        return f"http://{self._host}:{self._port}{self.PATH}"

    async def start(self) -> None:
        """Bind an ephemeral loopback port. Idempotent."""
        async with self._start_lock:
            if self._server is not None:
                return
            self._server = await asyncio.start_server(
                self._handle, self._host, 0, limit=MAX_HEAD_BYTES
            )
            sockets = self._server.sockets or ()
            self._port = int(sockets[0].getsockname()[1])
            logger.debug("service-token endpoint listening at %s", self.url)

    async def stop(self) -> None:
        server, self._server = self._server, None
        self._port = 0
        self._grants.clear()
        if server is None:
            return
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def grant(
        self, extension_id: str, service_url: str, *, secret: str | None = None
    ) -> dict[str, str]:
        """Let one extension process ask for its service's token.

        Returns the environment to start it with. A second grant for
        the same extension replaces the first, so a restarted process
        does not inherit its predecessor's secret.

        ``secret`` is the process's own, when the installer made one to
        share with the host model's channel
        (:class:`~ember_code.extensions.host.channel.HostChannel`): one
        secret names one process, wherever it presents it.
        """
        await self.start()
        secret = secret or secrets.token_urlsafe(32)
        self._grants[extension_id] = ServiceGrant(
            extension_id=extension_id, service_url=service_url, secret=secret
        )
        return {
            ENV_SERVICE_URL: service_url,
            ENV_TOKEN_URL: self.url,
            ENV_TOKEN_SECRET: secret,
        }

    def revoke(self, extension_id: str) -> None:
        self._grants.pop(extension_id, None)

    # ── One connection ────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            reply = await self._reply_to(reader)
            writer.write(reply.encode_head())
            writer.write(reply.body)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception:  # noqa: BLE001 - a connection must not take the listener
            logger.exception("service-token endpoint failed while answering")
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()

    async def _reply_to(self, reader: asyncio.StreamReader) -> HttpReply:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError):
            return self._error(408, "Request Timeout", "the request did not arrive")
        except (asyncio.LimitOverrunError, ValueError):
            return self._error(431, "Request Header Fields Too Large", "the request is too large")

        lines = head.decode("latin-1").split("\r\n")
        parts = (lines[0] if lines else "").split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            return self._error(400, "Bad Request", "not an HTTP/1.1 request")
        method, target, _ = parts
        headers = self._headers(lines[1:])

        if method != "GET":
            return self._error(405, "Method Not Allowed", "GET only", allow="GET")
        if headers.get("host", "") not in (
            f"{self._host}:{self._port}",
            f"[{self._host}]:{self._port}",
        ):
            return self._error(421, "Misdirected Request", "this is not the host you asked for")
        if target.split("?", 1)[0] != self.PATH:
            return self._error(404, "Not Found", "no such endpoint")

        grant = self._grant_for(headers.get("authorization", ""))
        if grant is None:
            return self._error(
                401,
                "Unauthorized",
                "the secret is not one this host issued",
                challenge='Bearer realm="igni-service-token"',
            )

        named = headers.get(Principal.TOKEN_HEADER.lower(), "")
        if named and not self._is_signed_in(named):
            return self._error(
                403,
                "Forbidden",
                f"this machine serves only the person signed in on it, not {named}",
            )

        try:
            token = await self._exchange.token_for(grant.service_url)
        except NotSignedIn as exc:
            return self._error(503, "Service Unavailable", str(exc))
        except TokenExchangeFailed as exc:
            logger.warning("token for %s's service failed: %s", grant.extension_id, exc)
            return self._error(502, "Bad Gateway", str(exc))

        body = TokenReply(
            access_token=token.access_token,
            expires_in=self._exchange.remaining(token),
            service_url=grant.service_url,
        ).model_dump_json()
        return HttpReply(200, "OK", body=body.encode(), content_type=_JSON)

    def _is_signed_in(self, named: str) -> bool:
        """Whether ``person:<id>`` is who this backend serves.

        ``ext.service.for_(caller)`` names its caller; a single-person
        backend has one, and a token for anybody else — another person,
        an app — is not something it can hand out (docs/
        EXTENSION_ROUTING_AND_CONCURRENCY.md §6).
        """
        if self._principal is None:
            return False
        principal = self._principal()
        return principal is not None and hmac.compare_digest(principal.ref, named)

    def _grant_for(self, authorization: str) -> ServiceGrant | None:
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            return None
        presented = presented.strip()
        # Every grant is compared, in constant time, whatever matches:
        # how long a refusal takes says nothing about the secrets.
        found: ServiceGrant | None = None
        for grant in self._grants.values():
            if hmac.compare_digest(grant.secret.encode(), presented.encode()):
                found = grant
        return found

    @staticmethod
    def _headers(lines: list[str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in lines:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return headers

    @staticmethod
    def _error(
        status: int, reason: str, message: str, *, allow: str = "", challenge: str = ""
    ) -> HttpReply:
        extra: dict[str, str] = {}
        if allow:
            extra["Allow"] = allow
        if challenge:
            extra["WWW-Authenticate"] = challenge
        body = ErrorReply(error=message).model_dump_json().encode()
        return HttpReply(status, reason, body=body, content_type=_JSON, extra=extra)
