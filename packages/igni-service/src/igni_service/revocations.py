"""Who may do nothing any more: igni's revocations, kept fresh per organisation.

igni's service-identity §21. A person removed from an organisation —
deactivated, erased, their sessions withdrawn — an app or a space
deleted: nothing they hold works from that moment, not for the ten
minutes their access token would otherwise live. igni publishes
revocations to each organisation's services::

    GET /v1/revocations?since=<cursor>&limit=500&wait=25     (the service's own token)
    → {"revocations": [{"kind", "org_id" | null, "subject_id", "incarnation" | null,
                        "not_before", "reason"}], "cursor"}

A revocation is a **watermark**: a token whose `sub` is `subject_id`, of
that kind, in organisation `org_id` (any, when it is null) and issued at
or before `not_before` is refused. A person added back signs in again,
after the watermark.

:class:`RevocationFeed` follows one organisation's revocations per
organisation this service sees a token for — long-polled (`wait=25`),
asked again at once while pages come back full, and every
:attr:`RevocationFeed.POLL_SECONDS` while igni fails. While it runs the
check **fails closed**: an organisation's token is refused
(:class:`RevocationsStale`, a 503) until its revocations have been read
once, and whenever they have not been refreshed for
:attr:`RevocationList.max_staleness` seconds. A verifier whose service
never started the feed checks nothing.
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
    #: Unix seconds: a token issued at or before it is refused.
    not_before: float
    #: ``removed``, ``inactive``, ``erased``, ``sessions_withdrawn``.
    reason: str = ""


class RevocationPage(BaseModel):
    revocations: list[Revocation] = Field(default_factory=list)
    cursor: str | None = None


class Revoked(TokenRefused):
    """A token igni has revoked: refused like any other bad token."""


class RevocationsStale(IgniUnavailable):
    """An organisation's revocations cannot be trusted now: nothing of it is accepted."""


class RevocationList:
    """The watermarks, and how fresh each organisation's are."""

    #: The kind a token's ``token_use`` names.
    _KIND: ClassVar[dict[str, RevokedKind]] = {
        "user": RevokedKind.PERSON,
        "app": RevokedKind.APP,
        "space": RevokedKind.SPACE,
    }
    #: igni keeps a revocation 31 days, past the longest a token lives.
    KEPT_SECONDS: ClassVar[float] = 31 * 86400.0

    def __init__(self, *, max_staleness: float = 30.0, clock: Clock | None = None) -> None:
        self.max_staleness = max_staleness
        self._clock = clock or Clock()
        #: ``(kind, org or None, subject)`` → the latest ``not_before``.
        self._watermarks: dict[tuple[RevokedKind, str | None, str], float] = {}
        #: Per organisation: the cursor to ask from, and when igni last answered.
        self.cursors: dict[str, str] = {}
        self._heard_at: dict[str, float] = {}

    def heard(self, org_id: str, page: RevocationPage) -> None:
        """igni answered for ``org_id``: take what is new, and note it is fresh."""
        for revocation in page.revocations:
            key = (revocation.kind, revocation.org_id, revocation.subject_id)
            if revocation.not_before > self._watermarks.get(key, float("-inf")):
                self._watermarks[key] = revocation.not_before
        if page.cursor is not None:
            self.cursors[org_id] = page.cursor
        self._heard_at[org_id] = self._clock.monotonic()
        self._prune()

    def _prune(self) -> None:
        oldest = self._clock.wall() - self.KEPT_SECONDS
        for key in [k for k, at in self._watermarks.items() if at < oldest]:
            del self._watermarks[key]

    def has_heard(self, org_id: str) -> bool:
        return org_id in self._heard_at

    def stale(self, org_id: str) -> bool:
        heard = self._heard_at.get(org_id)
        return heard is None or self._clock.monotonic() - heard > self.max_staleness

    def check(self, claims: dict[str, Any]) -> None:
        """Raise `Revoked` for a token a watermark covers, `RevocationsStale`
        when its organisation's revocations are not fresh."""
        org = str(claims.get("org") or "")
        if org and self.stale(org):
            raise RevocationsStale(
                f"igni's revocations for {org} are not fresh (bound {self.max_staleness:g}s)"
            )
        kind = self._KIND.get(str(claims.get("token_use") or ""))
        subject = str(claims.get("sub") or "")
        if kind is None or not subject:
            return
        try:
            issued = float(claims["iat"])
        except (KeyError, TypeError, ValueError):
            raise TokenRefused() from None
        for scope in ((org or None), None):
            watermark = self._watermarks.get((kind, scope, subject))
            if watermark is not None and issued <= watermark:
                raise Revoked()

    def __len__(self) -> int:
        return len(self._watermarks)


class RevocationFeed:
    """Follows each organisation's revocations for as long as it runs."""

    #: How long igni is asked to hold a request with nothing new (0–30).
    WAIT_SECONDS: ClassVar[float] = 25.0
    #: Between tries while igni fails.
    POLL_SECONDS: ClassVar[float] = 2.0
    #: Revocations per page; a full page is followed at once by the next.
    LIMIT: ClassVar[int] = 500

    def __init__(
        self,
        tokens: ServiceTokens,
        revocations: RevocationList,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._tokens = tokens
        self.revocations = revocations
        self._sleep = sleep or asyncio.sleep
        self._following: dict[str, asyncio.Task[None]] = {}
        self._first: dict[str, asyncio.Event] = {}
        self._stop: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    async def run(self, stop: asyncio.Event) -> None:
        """From now until ``stop``, every organisation seen is followed."""
        self._stop = stop
        try:
            await stop.wait()
        finally:
            self._stop = None
            tasks = list(self._following.values())
            self._following.clear()
            self._first.clear()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def check(self, claims: dict[str, Any]) -> None:
        """The verifier's check: follow the token's organisation, then ask the list."""
        if not self.running:
            return
        org = str(claims.get("org") or "")
        if org and not self.revocations.has_heard(org):
            first = self._follow(org)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(first.wait(), self.revocations.max_staleness)
        self.revocations.check(claims)

    def _follow(self, org_id: str) -> asyncio.Event:
        first = self._first.setdefault(org_id, asyncio.Event())
        if org_id not in self._following:
            self._following[org_id] = asyncio.create_task(self._loop(org_id, first))
        return first

    async def _loop(self, org_id: str, first: asyncio.Event) -> None:
        while self.running:
            wait = self.WAIT_SECONDS if first.is_set() else 0.0
            started = time.monotonic()
            try:
                page = await self.pull(org_id, wait=wait)
            except IgniUnavailable as exc:
                logger.info("revocations of %s not refreshed: %s", org_id, exc)
                await self._sleep(self.POLL_SECONDS)
                continue
            self.revocations.heard(org_id, page)
            first.set()
            if wait and not page.revocations and time.monotonic() - started < wait / 2:
                # Answered at once with nothing: igni did not hold the
                # request, so ask again on the polling interval.
                await self._sleep(self.POLL_SECONDS)

    async def pull(self, org_id: str, *, wait: float = 0.0) -> RevocationPage:
        """One read of ``org_id``'s revocations since its cursor. Raises `IgniUnavailable`."""
        params: dict[str, str] = {"limit": str(self.LIMIT), "wait": f"{wait:g}"}
        cursor = self.revocations.cursors.get(org_id)
        if cursor is not None:
            params["since"] = cursor
        response = await self._tokens.request(
            "GET",
            IgniPaths.REVOCATIONS,
            org_id=org_id,
            params=params,
            timeout=wait + 10.0,
        )
        if response.status_code == 304:
            return RevocationPage(cursor=cursor)
        if response.status_code != 200:
            raise IgniUnavailable(f"igni server answered {response.status_code}")
        try:
            return RevocationPage.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise IgniUnavailable("igni server's revocations are unreadable") from exc


__all__ = [
    "Revocation",
    "RevocationFeed",
    "RevocationList",
    "RevocationPage",
    "RevocationsStale",
    "Revoked",
    "RevokedKind",
]
