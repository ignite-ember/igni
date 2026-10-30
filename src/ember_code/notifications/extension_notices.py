"""``notifications/igni/notify``: an extension telling the person at this machine.

igni server's ``docs/service-identity.md`` §10.2. The extension's
process is an MCP server on a stdio connection the host opened, so it
says "your export is ready" with an MCP notification on that
connection, and this decides whether to keep it.

**From that extension, and only about its own pages.** Which extension
sent a notice is the connection it arrived on — the host named the
connection, and nothing in the params can change it — so one extension
cannot post as another. Its ``target.page`` must be one of the pages
that extension's manifest declares: a notice is opened by going there,
and an extension sending people to another's page would be borrowing
that page's authority.

Kept by :class:`~ember_code.notifications.local_store.LocalNotificationStore`;
never sent to igni server.
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ember_code.extensions.installer import SERVER_PREFIX
from ember_code.extensions.registry import ExtensionRegistry
from ember_code.notifications.schemas import (
    ExtensionNotice,
    FloorNotification,
    NotificationTarget,
)

logger = logging.getLogger(__name__)


class NoticeVerdict(StrEnum):
    STORED = "stored"
    #: Its ``event_key`` was already kept for this extension.
    DUPLICATE = "duplicate"
    REFUSED = "refused"


class NoticeOutcome(BaseModel):
    verdict: NoticeVerdict
    reason: str = ""
    notification: FloorNotification | None = None


class LocalNoticeSink(Protocol):
    """Where an accepted notice goes. :class:`NotificationCenter` is one."""

    def keep_local(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        """Keep it; ``None`` when its event was already kept."""
        ...


class ExtensionNoticeReceiver:
    """Accepts or refuses ``notifications/igni/notify`` from extension processes."""

    METHOD = "notifications/igni/notify"

    def __init__(self, *, registry: ExtensionRegistry, sink: LocalNoticeSink) -> None:
        self._registry = registry
        self._sink = sink

    @staticmethod
    def extension_for(server: str) -> str | None:
        """The extension an MCP connection belongs to, from its name."""
        if not server.startswith(SERVER_PREFIX):
            return None
        return server[len(SERVER_PREFIX) :] or None

    def __call__(self, server: str, params: dict[str, Any]) -> None:
        """The :class:`~ember_code.core.mcp.notification_tap.McpNotificationRouter` handler."""
        extension_id = self.extension_for(server)
        if extension_id is None:
            logger.warning("%s from %s, which is not an extension: dropped", self.METHOD, server)
            return
        outcome = self.receive(extension_id, params)
        if outcome.verdict is NoticeVerdict.REFUSED:
            logger.warning("%s from %s refused: %s", self.METHOD, extension_id, outcome.reason)

    def receive(self, extension_id: str, params: Any) -> NoticeOutcome:
        """Decide about one notice that arrived on ``extension_id``'s connection."""
        entry = self._registry.get(extension_id)
        if entry is None:
            return NoticeOutcome(
                verdict=NoticeVerdict.REFUSED, reason=f"{extension_id} is not installed"
            )
        try:
            notice = ExtensionNotice.model_validate(params)
        except ValidationError as exc:
            return NoticeOutcome(verdict=NoticeVerdict.REFUSED, reason=f"malformed: {exc}")
        pages = {page.kind for page in entry.manifest.contributes.pages}
        if notice.target.page not in pages:
            return NoticeOutcome(
                verdict=NoticeVerdict.REFUSED,
                reason=(
                    f"target.page {notice.target.page!r} is not one of {extension_id}'s "
                    f"pages ({', '.join(sorted(pages)) or 'it declares none'})"
                ),
            )
        kept = self._sink.keep_local(extension_id, notice)
        if kept is None:
            return NoticeOutcome(verdict=NoticeVerdict.DUPLICATE)
        return NoticeOutcome(verdict=NoticeVerdict.STORED, notification=kept)


class FaultNotices:
    """Tells the person when an extension stops responding.

    :class:`~ember_code.extensions.faults.ExtensionFaults` decides; this
    says it, as a notice from that extension on this machine, opening its
    first page. An extension with no page is not told about here — the
    extensions panel and the host model say it is faulty either way.
    """

    KIND = "extension.not_responding"

    def __init__(self, registry: ExtensionRegistry, sink: LocalNoticeSink) -> None:
        self._registry = registry
        self._sink = sink

    def told(self, extension_id: str, reason: str) -> None:
        entry = self._registry.get(extension_id)
        if entry is None or not entry.manifest.contributes.pages:
            logger.info("%s is faulty and has no page to point a notice at", extension_id)
            return
        notice = ExtensionNotice(
            kind=self.KIND,
            title=f"{entry.manifest.display_name} isn't responding"[:200],
            body=reason[:1000],
            target=NotificationTarget(page=entry.manifest.contributes.pages[0].kind),
            event_key=f"{self.KIND}:{time.time_ns()}",
        )
        self._sink.keep_local(extension_id, notice)
