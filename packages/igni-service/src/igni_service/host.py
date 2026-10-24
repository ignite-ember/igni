"""igni, as one organisation sees it: what `igni.host(org_id)` answers."""

from __future__ import annotations

from igni_service.directory import Directory, DirectoryClient
from igni_service.notifications import Delivered, Notification, Notifier


class Host:
    """The floor a service stands on, for one organisation.

    Cheap to make: it holds no state of its own. Tokens, caches and the
    HTTP client are the service's, shared by every `Host`.
    """

    def __init__(self, org_id: str, *, notifier: Notifier, directory: DirectoryClient) -> None:
        if not org_id:
            raise ValueError("a host is for one organisation, and none was named")
        self._org_id = org_id
        self._notifier = notifier
        self.directory = Directory(directory, org_id)

    @property
    def org_id(self) -> str:
        return self._org_id

    async def notify(self, *notifications: Notification) -> Delivered:
        """Tell people in this organisation that something happened.

        Any number of notifications, in as few calls as igni accepts.
        Raises `IgniUnavailable` when igni could not take them after
        retrying, and `NotificationRefused` when it read them and said no.
        A caller for whom notifying is best effort catches `IgniError`.
        """
        return await self._notifier.send(self._org_id, notifications)
