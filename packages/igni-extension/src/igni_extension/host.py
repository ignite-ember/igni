"""`ext.host`: igni as an extension sees it, one object per part of the app.

The host object model (`docs/HOST_OBJECT_MODEL.md` in igni) gives every
part of igni an object here with properties, signals and methods —
`host.sessions`, `host.chat`, `host.workspace` and the rest. They are
generated from one schema and arrive together; until then the one object
built is `host.notifications`, whose `notify` speaks the MCP notification
the host already understands.
"""

from __future__ import annotations

from igni_extension.notifications import LocalNotification, NotifyNotification
from igni_extension.session import HostSession


class Notifications:
    """`host.notifications`: telling the person at this machine something."""

    def __init__(self, session: HostSession) -> None:
        self._session = session

    async def notify(self, notification: LocalNotification) -> None:
        """Show `notification` to the person using this igni.

        It never leaves the machine. Sent before the host has connected,
        it is held and delivered as soon as it does.
        """
        await self._session.send(NotifyNotification.of(notification))


class Host:
    """The root of the host object model."""

    def __init__(self, session: HostSession) -> None:
        self.notifications = Notifications(session)
