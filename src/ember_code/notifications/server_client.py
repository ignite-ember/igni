"""igni server's notification routes, called with the person's session.

igni server's ``docs/service-identity.md`` §10.1. The host holds the
igni session, so it is the host that lists, counts and marks; neither
an extension's process nor its UI ever sees these calls. The server
URL and the session are read on every call, because signing in can
change both.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from pydantic import ValidationError

from ember_code.core.config.endpoint import is_configured
from ember_code.extensions.service_token import IgniSession, NotSignedIn
from ember_code.notifications.schemas import (
    MarkReadRequest,
    NotificationCounts,
    PortalMe,
    ServerNotification,
    ServerNotificationPage,
)


class NotificationsUnavailable(Exception):
    """igni server did not answer a notification call, and why."""


class SignedInServer:
    """Where igni server is and who is signed in to it, read fresh each time."""

    def __init__(
        self,
        *,
        api_url: Callable[[], str | None],
        session: Callable[[], IgniSession | None],
    ) -> None:
        self._api_url = api_url
        self._session = session

    def current(self) -> tuple[str, IgniSession]:
        """``(api_url, session)``.

        Raises:
            NotSignedIn: nobody is signed in, or no server is configured.
        """
        session = self._session()
        if session is None:
            raise NotSignedIn("nobody is signed in to igni")
        api_url = self._api_url()
        if not is_configured(api_url):
            raise NotSignedIn("no igni server is configured")
        assert api_url is not None
        return api_url.rstrip("/"), session


class IgniNotificationsClient:
    """``GET /v1/notifications``, ``…/counts`` and ``POST …/read``."""

    LIST_PATH = "/v1/notifications"
    COUNTS_PATH = "/v1/notifications/counts"
    READ_PATH = "/v1/notifications/read"
    #: Who the person is — read for the organisations they belong to.
    ME_PATH = "/v1/portal/me"
    #: How many rows one listing asks for.
    PAGE = 50

    def __init__(
        self,
        server: SignedInServer,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._server = server
        self._transport = transport
        self._timeout = timeout

    async def listing(self, *, limit: int = PAGE) -> list[ServerNotification]:
        """The person's notifications, read and unread, newest first."""
        response = await self._call("GET", self.LIST_PATH, params={"limit": str(limit)})
        try:
            return ServerNotificationPage.model_validate_json(response.content).notifications
        except ValidationError as exc:
            raise NotificationsUnavailable(f"the notification list is malformed: {exc}") from exc

    async def counts(self) -> NotificationCounts:
        response = await self._call("GET", self.COUNTS_PATH)
        try:
            return NotificationCounts.model_validate_json(response.content)
        except ValidationError as exc:
            raise NotificationsUnavailable(f"the notification counts are malformed: {exc}") from exc

    async def organisations(self) -> dict[str, str]:
        """Every organisation the person belongs to, id to name."""
        response = await self._call("GET", self.ME_PATH)
        try:
            return PortalMe.model_validate_json(response.content).names()
        except ValidationError as exc:
            raise NotificationsUnavailable(f"the account is malformed: {exc}") from exc

    async def mark_read(self, request: MarkReadRequest) -> None:
        await self._call("POST", self.READ_PATH, json=request.body())

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        api_url, session = self._server.current()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    f"{api_url}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
        except httpx.HTTPError as exc:
            raise NotificationsUnavailable(f"igni could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise NotificationsUnavailable(f"igni refused {method} {path} ({response.status_code})")
        return response
