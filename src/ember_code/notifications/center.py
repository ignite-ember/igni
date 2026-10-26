"""The floor's notifications: both paths, one list, one count per extension.

igni server's ``docs/service-identity.md`` §10. Telling people things is
the floor's job, so neither an extension's process nor its UI keeps a
"waiting" count of its own. This keeps it:

* **To other people** — rows igni server delivered to whoever is signed
  in. Read on start-up, on sign-in, whenever the stream wakes the host
  and on every reconnect (:class:`NotificationStreamFollower`), and
  when the person opens the list. Never on a timer. Read state lives
  on igni server, so marking one read is a call there.
* **To yourself** — notices an extension's own process told this host
  (:class:`ExtensionNoticeReceiver`). Kept and marked here, in
  :class:`LocalNotificationStore`; never sent anywhere.

Whenever either moves, the client is told the new counts on the
``notifications_changed`` push, which is what every badge reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from ember_code.core.activity import background
from ember_code.extensions.service_token import NotSignedIn
from ember_code.notifications.local_store import LocalNotificationStore
from ember_code.notifications.schemas import (
    ExtensionNotice,
    FloorNotification,
    MarkReadRequest,
    NotificationCounts,
    NotificationsListResult,
    NotificationsReadResult,
    ServerNotification,
)
from ember_code.notifications.server_client import (
    IgniNotificationsClient,
    NotificationsUnavailable,
    SignedInServer,
)
from ember_code.notifications.stream import Backoff, NotificationStreamFollower

logger = logging.getLogger(__name__)

_OLDEST = datetime.min.replace(tzinfo=UTC)


class NotificationCenter:
    """Merges igni server's notifications with this machine's own."""

    #: How many rows one list answers with.
    LIMIT = 100

    def __init__(
        self,
        *,
        server: SignedInServer,
        local: LocalNotificationStore,
        announce: Callable[[NotificationCounts], None],
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """
        Args:
            announce: Tell the client the counts moved. Called with the
                merged counts every time either half changes.
            transport, backoff, sleep: For tests — a fake igni server
                and a clock that does not wait.
        """
        self._server = server
        self._client = IgniNotificationsClient(server, transport=transport)
        self._local = local
        self._announce = announce
        self._follower = NotificationStreamFollower(
            server,
            on_reconnect=self.wake,
            on_wake=self.wake,
            transport=transport,
            backoff=backoff,
            sleep=sleep,
        )
        # igni server's half, as last read.
        self._server_rows: list[ServerNotification] = []
        self._server_counts = NotificationCounts()
        self._server_error = ""
        # The organisations the signed-in person belongs to, id to name,
        # read once per sign-in: it is what puts an organisation's name
        # on a row when there is more than one.
        self._organisations: dict[str, str] | None = None
        # One refresh in flight at most, and at most one queued behind
        # it: ten wakes during a slow read are one more read, not ten.
        self._refreshing: asyncio.Task[None] | None = None
        self._again = False

    @property
    def follower(self) -> NotificationStreamFollower:
        return self._follower

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Read the list, and follow the stream for what changes after.

        Read here rather than when the stream opens, so a server whose
        stream cannot be reached is still read once.
        """
        self.wake()
        self._follower.start()

    async def signed_in(self) -> None:
        """Somebody signed in: read their list, follow their stream."""
        self._organisations = None
        self.wake()
        await self._follower.restart()

    def schedule_signed_in(self) -> None:
        """:meth:`signed_in`, for a synchronous sign-in listener."""
        background.start(self.signed_in(), holder="notifications", reason="a sign-in")

    async def resumed(self, _restored: object = None) -> None:
        """The VM was restored from a snapshot: catch up, on a fresh connection."""
        self.wake()
        await self._follower.reconnect()

    async def signed_out(self) -> None:
        """Nobody is signed in: forget their notifications."""
        await self._follower.stop()
        self._server_rows = []
        self._server_counts = NotificationCounts()
        self._server_error = ""
        self._organisations = None
        self._announce(self.counts())

    async def stop(self) -> None:
        await self._follower.stop()
        task, self._refreshing = self._refreshing, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ── igni server's half ───────────────────────────────────────

    def wake(self) -> None:
        """Something may have changed on igni server: read it again, soon."""
        if self._refreshing is not None and not self._refreshing.done():
            self._again = True
            return
        self._refreshing = background.start(
            self._refresh_until_settled(), holder="notifications", reason="reading notifications"
        )

    async def _refresh_until_settled(self) -> None:
        while True:
            self._again = False
            await self.refresh()
            if not self._again:
                return

    async def refresh(self) -> None:
        """Read igni server's list and counts now, and tell the client."""
        try:
            rows, counts = await asyncio.gather(
                self._client.listing(limit=self.LIMIT), self._client.counts()
            )
        except NotSignedIn:
            self._server_rows, self._server_counts, self._server_error = (
                [],
                NotificationCounts(),
                "",
            )
        except NotificationsUnavailable as exc:
            # The last rows read stay: "could not ask" is not "nothing
            # is waiting", and a badge that drops to zero whenever the
            # network does is a badge nobody believes.
            self._server_error = str(exc)
            logger.info("notifications not refreshed: %s", exc)
        else:
            self._server_rows, self._server_counts, self._server_error = rows, counts, ""
        self._announce(self.counts())

    # ── This machine's half ──────────────────────────────────────

    def keep_local(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        """Keep a notice an extension told this host. ``None`` for a repeat."""
        kept = self._local.add(source, notice)
        if kept is not None:
            self._announce(self.counts())
        return kept

    # ── What the client asks ─────────────────────────────────────

    def counts(self) -> NotificationCounts:
        """Unread, from both halves, per extension.

        igni server's half only while somebody is signed in: signing out
        by ``/logout`` reaches no listener here, and the last person's
        count must not outlive their session on a badge.
        """
        try:
            self._server.current()
        except NotSignedIn:
            self._server_rows, self._server_counts = [], NotificationCounts()
        return self._server_counts.plus(self._local.counts())

    async def listing(self) -> NotificationsListResult:
        """Both halves, newest first, read from igni server now."""
        await self.refresh()
        organisations = await self._organisation_names() if self._server_rows else {}
        merged = [FloorNotification.from_server(row, organisations) for row in self._server_rows]
        merged.extend(self._local.listing(limit=self.LIMIT))
        merged.sort(key=self._newest_first, reverse=True)
        return NotificationsListResult(
            notifications=merged[: self.LIMIT],
            counts=self.counts(),
            server_error=self._server_error,
        )

    async def _organisation_names(self) -> dict[str, str]:
        """The person's organisations, read once per sign-in.

        Not reading them costs a name on a row, never the list: a failure
        answers nothing and is asked again next time.
        """
        if self._organisations is None:
            try:
                self._organisations = await self._client.organisations()
            except (NotSignedIn, NotificationsUnavailable) as exc:
                logger.info("organisations not read: %s", exc)
                return {}
        return self._organisations

    @staticmethod
    def _newest_first(notification: FloorNotification) -> datetime:
        at = notification.created_at
        if at is None:
            return _OLDEST
        # A server that writes no offset means UTC; comparing a naive time
        # with an aware one would raise.
        return at if at.tzinfo is not None else at.replace(tzinfo=UTC)

    async def mark_read(self, request: MarkReadRequest) -> NotificationsReadResult:
        """Mark these ids, this extension's, or (neither) everything.

        Each half marks its own: a local id here, a server id on igni
        server. Nothing local is ever sent there.
        """
        self._local.mark_read(ids=request.ids, source=request.source)
        error = ""
        server_ids = (
            None if request.ids is None else [i for i in request.ids if not self._local.owns(i)]
        )
        if server_ids is None or server_ids:
            try:
                await self._client.mark_read(MarkReadRequest(ids=server_ids, source=request.source))
            except NotSignedIn:
                pass
            except NotificationsUnavailable as exc:
                error = str(exc)
        if not error:
            await self.refresh()
            error = self._server_error
        else:
            self._announce(self.counts())
        return NotificationsReadResult(counts=self.counts(), server_error=error)
