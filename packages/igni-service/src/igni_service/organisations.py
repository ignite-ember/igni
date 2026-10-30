"""igni calling this service about a whole organisation: it was deleted.

When igni deletes an organisation it tells every approved service, with
a token it signs for that service (`token_use` = `igni`, `aud` = the
service's URL, `org` = the organisation):

* `DELETE /v1/igni/organisations/{org_id}` — the organisation is gone.
  Stop routing anything to its data. Idempotent: 204 whether or not the
  service had heard of it, because igni keeps telling a service until it
  confirms — a service that was down when the organisation went is told
  when it is back.

The service writes one handler (`@igni.on_organisation_deleted`), which
receives the organisation's id. Without one the route answers 501, which
igni treats as unconfirmed and retries: a service that keeps nothing per
organisation says so with a handler that does nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Response, status

from igni_service.tokens import IgniCall
from igni_service.urls import IgniPaths

OrganisationDeletedHandler = Callable[[str], Awaitable[None]]
IgniCaller = Callable[..., Awaitable[IgniCall]]


class OrganisationHandlers:
    """The service's handler for a deleted organisation, registered by decorator."""

    def __init__(self) -> None:
        self.deleted: OrganisationDeletedHandler | None = None

    def on_deleted(self, handler: OrganisationDeletedHandler) -> OrganisationDeletedHandler:
        self.deleted = handler
        return handler


class OrganisationRoutes:
    """The route igni calls when an organisation is deleted."""

    def __init__(self, handlers: OrganisationHandlers, igni_caller: IgniCaller) -> None:
        self._handlers = handlers
        self._igni_caller = igni_caller

    def router(self) -> APIRouter:
        router = APIRouter(prefix=IgniPaths.ORGANISATIONS, tags=["igni"])
        handlers = self._handlers
        caller = self._igni_caller

        @router.delete(
            "/{org_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="The organisation was deleted in igni",
        )
        async def organisation_deleted(org_id: str, call: IgniCall = Depends(caller)) -> Response:
            # The token is for one organisation; it says nothing about another.
            if call.org_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="The token is for another organisation.",
                )
            if handlers.deleted is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="This service has no handler for a deleted organisation.",
                )
            await handlers.deleted(org_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return router
