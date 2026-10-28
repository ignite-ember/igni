"""Who may do nothing any more: igni's revocations, kept fresh.

igni's service-identity §21. A person removed from an organisation —
deactivated, erased, their sessions withdrawn — an app or a space
deleted: nothing they hold works from that moment, not for the ten
minutes their access token would otherwise live. A service follows the
revocations of every organisation it serves on one cursor and one long
poll::

    GET /v1/organisations/served                          → {"organisations": [{"org_id", "name"}]}
    GET /v1/revocations?scope=served&since=<cursor>&wait=25
        If-None-Match: "<cursor>"                         → 304 when nothing is new
    → {"revocations": [{"kind", "org_id" | null, "subject_id", "incarnation" | null,
                        "not_before_ms", "reason"}], "cursor"}

A revocation is a **watermark**: a token of that kind whose `sub` is
`subject_id`, in organisation `org_id` (any, when it is null), issued at
or before `not_before_ms` (its `iat_ms`) is refused. Someone added back
signs in again, after the watermark.

:class:`RevocationFeed` follows them for as long as the service runs —
long-polled, asked again at once after anything new, and every
:attr:`RevocationFeed.POLL_SECONDS` while igni fails. Both calls take any
of the service's tokens, and a service token names one organisation:
the feed starts once it knows one — from
:attr:`RevocationFeed.known_organisation` (``IGNI_SERVED_ORGANISATION``),
or the first token it is asked to check. While it runs the check
**fails closed**: tokens are refused (:class:`RevocationsStale`, a 503)
until the revocations have been read once, and whenever they go
unrefreshed for :attr:`RevocationList.max_staleness` seconds. A verifier
whose service never started the feed checks nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.clock import Clock
from igni_service.errors import IgniUnavailable
from igni_service.tokens import TokenRefused
from igni_service.urls import IgniPaths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from igni_service.service_tokens import ServiceTokens

logger = logging.getLogger(__name__)


class RevokedKind(StrEnum):
    PERSON = "person"
    APP = "app"
    SPACE = "space"


class Revocation(BaseModel):
    """One watermark, as igni publishes it."""

    model_config = ConfigDict(frozen=True)

    kind: RevokedKind
    #: Its organisation; ``None`` for every organisation (sessions withdrawn).
    org_id: str | None = None
    #: The token's ``sub``: a person's user id, an app's or a space's id.
    subject_id: str
    #: A person's membership revoked (§11.3); informational here.
    incarnation: str | None = None
    #: Unix milliseconds: a token issued (``iat_ms``) at or before it is refused.
    not_before_ms: int
    #: ``removed``, ``inactive``, ``erased``, ``sessions_withdrawn``.
    reason: str = ""


class RevocationPage(BaseModel):
    revocations: list[Revocation] = Field(default_factory=list)
    cursor: str | None = None


class ServedOrganisation(BaseModel):
    org_id: str
    name: str = ""


class ServedOrganisations(BaseModel):
    organisations: list[ServedOrganisation] = Field(default_factory=list)


class Revoked(TokenRefused):
    """A token igni has revoked: refused like any other bad token."""


class RevocationsStale(IgniUnavailable):
    """The revocations cannot be trusted now: no token is accepted."""


class RevocationList:
    """The watermarks, and how fresh they are."""

    #: The kind a token's ``token_use`` names.
    _KIND: ClassVar[dict[str, RevokedKind]] = {
        "user": RevokedKind.PERSON,
        "app": RevokedKind.APP,
        "space": RevokedKind.SPACE,
    }
    #: igni keeps a revocation 31 days, past the longest a token lives.
    KEPT_MS: ClassVar[int] = 31 * 86_400_000

    def __init__(self, *, max_staleness: float = 30.0, clock: Clock | None = None) -> None:
        self.max_staleness = max_staleness
        self._clock = clock or Clock()
        #: ``(kind, org or None, subject)`` → the latest ``not_before_ms``.
        self._watermarks: dict[tuple[RevokedKind, str | None, str], int] = {}
        self.cursor: str | None = None
        self._heard_at: float | None = None

    def heard(self, page: RevocationPage) -> None:
        """igni answered (a 304 is an empty page): take what is new; the list is fresh."""
        for revocation in page.revocations:
            key = (revocation.kind, revocation.org_id, revocation.subject_id)
            if revocation.not_before_ms > self._watermarks.get(key, -1):
                self._watermarks[key] = revocation.not_before_ms
        if page.cursor is not None:
            self.cursor = page.cursor
        self._heard_at = self._clock.monotonic()
        self._prune()

    def _prune(self) -> None:
        oldest = int(self._clock.wall() * 1000) - self.KEPT_MS
        for key in [k for k, at in self._watermarks.items() if at < oldest]:
            del self._watermarks[key]

    @property
    def has_heard(self) -> bool:
        return self._heard_at is not None

    @property
    def stale(self) -> bool:
        heard = self._heard_at
        return heard is None or self._clock.monotonic() - heard > self.max_staleness

    def check(self, claims: dict[str, Any]) -> None:
        """Raise `Revoked` for a token a watermark covers, `RevocationsStale`
        when the watermarks are not fresh."""
        if self.stale:
            raise RevocationsStale(
                f"igni's revocations are not fresh (bound {self.max_staleness:g}s)"
            )
        kind = self._KIND.get(str(claims.get("token_use") or ""))
        subject = str(claims.get("sub") or "")
        if kind is None or not subject:
            return
        issued_ms = self._issued_ms(claims)
        org = str(claims.get("org") or "") or None
        for scope in (org, None):
            watermark = self._watermarks.get((kind, scope, subject))
            if watermark is not None and issued_ms <= watermark:
                raise Revoked()

    @staticmethod
    def _issued_ms(claims: dict[str, Any]) -> int:
        """``iat_ms``; a token without it counts from the start of its ``iat`` second."""
        try:
            if claims.get("iat_ms") is not None:
                return int(claims["iat_ms"])
            return int(float(claims["iat"]) * 1000)
        except (KeyError, TypeError, ValueError):
            raise TokenRefused() from None

    def __len__(self) -> int:
        return len(self._watermarks)


class RevocationFeed:
    """Follows the revocations of every organisation this service serves."""

    #: How long igni is asked to hold a request with nothing new (0–30).
    WAIT_SECONDS: ClassVar[float] = 25.0
    #: Between tries while igni fails.
    POLL_SECONDS: ClassVar[float] = 2.0

    def __init__(
        self,
        tokens: ServiceTokens,
        revocations: RevocationList,
        *,
        known_organisation: str | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._tokens = tokens
        self.revocations = revocations
        #: An organisation whose service token reads the lists (any served one).
        self.known_organisation = known_organisation or None
        self._sleep = sleep or asyncio.sleep
        self._first = asyncio.Event()
        self._org_known = asyncio.Event()
        if self.known_organisation:
            self._org_known.set()
        self._stop: asyncio.Event | None = None
        #: What ``/v1/organisations/served`` last answered.
        self.served: list[ServedOrganisation] = []

    @property
    def running(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    async def run(self, stop: asyncio.Event) -> None:
        """From now until ``stop``: once an organisation is known, follow them all."""
        self._stop = stop
        follow = asyncio.create_task(self._follow())
        try:
            await stop.wait()
        finally:
            self._stop = None
            follow.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follow

    async def check(self, claims: dict[str, Any]) -> None:
        """The verifier's check: wait for the first read if need be, then ask the list."""
        if not self.running:
            return
        if not self.revocations.has_heard:
            org = str(claims.get("org") or "")
            if org and not self.known_organisation:
                self.known_organisation = org
                self._org_known.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._first.wait(), self.revocations.max_staleness)
        self.revocations.check(claims)

    async def _follow(self) -> None:
        await self._org_known.wait()
        while self.running:
            wait = self.WAIT_SECONDS if self._first.is_set() else 0.0
            started = time.monotonic()
            try:
                if not self.served:
                    self.served = await self.served_organisations()
                page = await self.pull(wait=wait)
            except IgniUnavailable as exc:
                logger.info("revocations not refreshed: %s", exc)
                await self._sleep(self.POLL_SECONDS)
                continue
            self.revocations.heard(page)
            self._first.set()
            if wait and not page.revocations and time.monotonic() - started < wait / 2:
                # Answered at once with nothing: igni did not hold the
                # request, so ask again on the polling interval.
                await self._sleep(self.POLL_SECONDS)

    async def served_organisations(self) -> list[ServedOrganisation]:
        response = await self._request(IgniPaths.SERVED)
        if response.status_code != 200:
            raise IgniUnavailable(f"igni server answered {response.status_code}")
        try:
            return ServedOrganisations.model_validate(response.json()).organisations
        except (ValueError, ValidationError) as exc:
            raise IgniUnavailable("igni server's served organisations are unreadable") from exc

    async def pull(self, *, wait: float = 0.0) -> RevocationPage:
        """One read since the cursor, for every served organisation. Raises `IgniUnavailable`."""
        params: dict[str, str] = {"scope": "served", "wait": f"{wait:g}"}
        headers: dict[str, str] = {}
        cursor = self.revocations.cursor
        if cursor is not None:
            params["since"] = cursor
            headers["If-None-Match"] = f'"{cursor}"'
        response = await self._request(
            IgniPaths.REVOCATIONS, params=params, headers=headers, timeout=wait + 10.0
        )
        if response.status_code == 304:
            return RevocationPage(cursor=cursor)
        if response.status_code != 200:
            raise IgniUnavailable(f"igni server answered {response.status_code}")
        try:
            return RevocationPage.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise IgniUnavailable("igni server's revocations are unreadable") from exc

    async def _request(self, path: str, **kwargs: Any) -> Any:
        org = self.known_organisation
        if not org:
            raise IgniUnavailable("no organisation known yet to ask igni with")
        return await self._tokens.request("GET", path, org_id=org, **kwargs)


__all__ = [
    "Revocation",
    "RevocationFeed",
    "RevocationList",
    "RevocationPage",
    "RevocationsStale",
    "Revoked",
    "RevokedKind",
    "ServedOrganisation",
]
