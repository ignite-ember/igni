"""Who may do nothing any more: igni's revocation list, kept fresh.

A person removed from an organisation can do nothing from that moment —
not for the ten minutes their access token would otherwise still live.
igni server publishes what it has revoked (``GET /v1/platform/revocations
?since=<cursor>&wait=<s>``); this service keeps the list in memory and
refuses any token it names:

* a **session** — the sign-in a token came from (its ``sid`` claim, or the
  ``grant`` a refresh token names);
* a **producer** in one organisation — ``person:<sub>``, ``app:<sub>`` or
  ``space:<sub>`` with ``org``;
* an **incarnation** — one life of a producer (its ``incarnation`` claim).

:class:`RevocationFeed` long-polls the list, and polls every
:attr:`RevocationFeed.POLL_SECONDS` where igni answers at once or not at
all. While it runs the list **fails closed**: once it has not heard from
igni for :attr:`RevocationList.max_staleness` seconds, every token is
refused (:class:`RevocationsStale`) until it has again — a list that
cannot be refreshed cannot say who was removed meanwhile. A verifier
whose service never started the feed checks nothing.

The wire format is igni server's (``docs/service-identity.md``):
``{"revocations": [{"kind", "id", "org_id"?, "expires_at"?}], "cursor"}``;
an entry is dropped once ``expires_at`` (Unix seconds) has passed, when no
token it could stop is still alive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.clock import Clock
from igni_service.errors import IgniUnavailable
from igni_service.server import IgniServer
from igni_service.token_endpoint import GrantRefused, TokenEndpoint
from igni_service.tokens import TokenRefused
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)


class RevocationKind(StrEnum):
    SESSION = "session"
    PRODUCER = "producer"
    INCARNATION = "incarnation"


class Revocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: RevocationKind
    #: The session's id, ``<kind>:<id>`` of the producer, or the incarnation's id.
    id: str
    #: A producer is revoked in one organisation.
    org_id: str | None = None
    #: When no token it could stop is alive any more (Unix seconds).
    expires_at: float | None = None


class RevocationPage(BaseModel):
    revocations: list[Revocation] = Field(default_factory=list)
    cursor: str | None = None


class Revoked(TokenRefused):
    """A token igni has revoked: refused like any other bad token."""


class RevocationsStale(IgniUnavailable):
    """The revocation list has not been refreshed within its bound: nothing is accepted."""


class RevocationList:
    """The revocations igni has published, and whether they are fresh enough to trust."""

    #: Producer kinds by ``token_use``.
    _PRODUCER: ClassVar[dict[str, str]] = {"user": "person", "app": "app", "space": "space"}

    def __init__(self, *, max_staleness: float = 30.0, clock: Clock | None = None) -> None:
        self.max_staleness = max_staleness
        self._clock = clock or Clock()
        self._sessions: dict[str, Revocation] = {}
        self._producers: dict[tuple[str, str | None], Revocation] = {}
        self._incarnations: dict[str, Revocation] = {}
        #: Set while a feed keeps the list: from then on it fails closed.
        self._armed_at: float | None = None
        self._heard_at: float | None = None
        self.cursor: str | None = None

    def arm(self) -> None:
        """A feed keeps this list now: it must stay fresh, or nothing passes."""
        self._armed_at = self._clock.monotonic()

    def disarm(self) -> None:
        self._armed_at = None

    def heard(self, page: RevocationPage) -> None:
        """igni answered: take what is new, and note that the list is fresh."""
        for revocation in page.revocations:
            self._add(revocation)
        if page.cursor is not None:
            self.cursor = page.cursor
        self._heard_at = self._clock.monotonic()
        self._prune()

    def _add(self, revocation: Revocation) -> None:
        if revocation.kind is RevocationKind.SESSION:
            self._sessions[revocation.id] = revocation
        elif revocation.kind is RevocationKind.PRODUCER:
            self._producers[(revocation.id, revocation.org_id)] = revocation
        else:
            self._incarnations[revocation.id] = revocation

    def _prune(self) -> None:
        now = self._clock.wall()
        for held in (self._sessions, self._producers, self._incarnations):
            for key in [
                k for k, r in held.items() if r.expires_at is not None and r.expires_at <= now
            ]:
                del held[key]  # type: ignore[arg-type]

    @property
    def stale(self) -> bool:
        if self._armed_at is None:
            return False
        since = self._heard_at if self._heard_at is not None else self._armed_at
        return self._clock.monotonic() - since > self.max_staleness

    def check(self, claims: dict[str, Any]) -> None:
        """Raise unless the token these claims are from may still be used.

        `RevocationsStale` while the list cannot be trusted; `Revoked` for
        a token it names.
        """
        if self.stale:
            raise RevocationsStale(
                f"igni's revocation list is more than {self.max_staleness:g}s old"
            )
        for session in (claims.get("sid"), claims.get("grant")):
            if session and str(session) in self._sessions:
                raise Revoked()
        incarnation = claims.get("incarnation")
        if incarnation and str(incarnation) in self._incarnations:
            raise Revoked()
        kind = self._PRODUCER.get(str(claims.get("token_use") or ""))
        if kind is not None and claims.get("sub"):
            producer = f"{kind}:{claims['sub']}"
            org = str(claims.get("org") or "") or None
            if (producer, org) in self._producers or (producer, None) in self._producers:
                raise Revoked()

    def __len__(self) -> int:
        return len(self._sessions) + len(self._producers) + len(self._incarnations)


class RevocationFeed:
    """Keeps a :class:`RevocationList` fresh: a long poll, else a poll every two seconds."""

    #: How long igni is asked to hold a request with nothing new.
    LONG_POLL_SECONDS: ClassVar[float] = 20.0
    #: Between polls when igni answers at once (no long poll) or fails.
    POLL_SECONDS: ClassVar[float] = 2.0
    #: The scope of the token the list is read with: it is no one organisation's.
    SCOPE: ClassVar[str] = "revocations"

    def __init__(
        self,
        igni: IgniServer,
        endpoint: TokenEndpoint,
        revocations: RevocationList,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._igni = igni
        self._endpoint = endpoint
        self.revocations = revocations
        self._sleep = sleep or asyncio.sleep
        self._token: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Until ``stop``: pull, and pull again — at once after a held answer."""
        self.revocations.arm()
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    page = await self.pull(wait=self.LONG_POLL_SECONDS)
                except IgniUnavailable as exc:
                    logger.info("revocation list not refreshed: %s", exc)
                    await self._sleep(self.POLL_SECONDS)
                    continue
                self.revocations.heard(page)
                held = time.monotonic() - started >= self.LONG_POLL_SECONDS / 2
                if not page.revocations and not held:
                    # igni answered at once with nothing: it does not hold
                    # requests, so ask again on the polling interval.
                    await self._sleep(self.POLL_SECONDS)
        finally:
            self.revocations.disarm()

    async def pull(self, *, wait: float = 0.0) -> RevocationPage:
        """One read of the list since its cursor. Raises `IgniUnavailable`."""
        params: dict[str, str] = {"wait": f"{wait:g}"}
        if self.revocations.cursor is not None:
            params["since"] = self.revocations.cursor
        for attempt in (1, 2):
            token = await self._read_token()
            try:
                response = await self._igni.http.get(
                    self._igni.url(IgniPaths.REVOCATIONS),
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=wait + self._igni_timeout(),
                )
            except httpx.HTTPError as exc:
                raise IgniUnavailable(f"could not reach igni server: {type(exc).__name__}") from exc
            if response.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if response.status_code != 200:
                raise IgniUnavailable(f"igni server answered {response.status_code}")
            try:
                return RevocationPage.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                raise IgniUnavailable("igni server's revocation list is unreadable") from exc
        raise IgniUnavailable("igni server refused this service the revocation list")

    def _igni_timeout(self) -> float:
        timeout = self._igni.http.timeout.read
        return float(timeout) if timeout is not None else 10.0

    async def _read_token(self) -> str:
        if self._token is None:
            try:
                issued = await self._endpoint.grant(
                    {"grant_type": "client_credentials", "scope": self.SCOPE}
                )
            except GrantRefused as exc:
                raise IgniUnavailable(f"igni server refused a token for the list: {exc}") from exc
            self._token = issued.access_token
        return self._token


__all__ = [
    "Revocation",
    "RevocationFeed",
    "RevocationKind",
    "RevocationList",
    "RevocationPage",
    "RevocationsStale",
    "Revoked",
]
