"""igni calling this service about one person: erasure and export.

An organisation admin's GDPR request is made in igni, and igni fans it
out to every approved service with a token it signs for that service
(`token_use` = `igni`, `aud` = the service's URL, `org` = the
organisation):

* `DELETE /v1/igni/subjects/{user_id}` — erase or redact the person's
  data in that organisation. Idempotent: 204 whether or not anything was
  there, so igni can run it again until every service confirms.
* `GET /v1/igni/subjects/{user_id}/export` — what the service holds
  about them, in sections of rows.

The service writes two handlers (`@igni.on_erase`, `@igni.on_export`).
A service that registers no handler answers 501, which igni reports as
unconfirmed under its name: a service that holds nothing about anybody
says so with a handler that does nothing, rather than by forgetting.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from igni_service.directory import DirectoryClient, Member
from igni_service.errors import IgniUnavailable
from igni_service.tokens import IgniCall
from igni_service.urls import IgniPaths

logger = logging.getLogger(__name__)

CellValue = str | int | float | bool | None


class Subject(BaseModel):
    """The person a request is about, in the one organisation it is about.

    `email` and `name` come from igni's directory when it can be asked —
    present or former member — and are empty when it cannot. Data matched
    on an address should then still be erased by id; igni repeats an
    erasure until every service confirms it.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    org_id: str
    email: str = ""
    name: str = ""
    member: Member | None = None


class ExportSection(BaseModel):
    """One section of a subject-access export. igni files it under the service's name."""

    name: str
    rows: list[dict[str, CellValue]] = Field(default_factory=list)


class SubjectExport(BaseModel):
    sections: list[ExportSection] = Field(default_factory=list)


EraseHandler = Callable[[Subject], Awaitable[None]]
ExportHandler = Callable[[Subject], Awaitable[list[ExportSection]]]
IgniCaller = Callable[..., Awaitable[IgniCall]]


class SubjectHandlers:
    """The service's two handlers, registered by decorator."""

    def __init__(self) -> None:
        self.erase: EraseHandler | None = None
        self.export: ExportHandler | None = None

    def on_erase(self, handler: EraseHandler) -> EraseHandler:
        self.erase = handler
        return handler

    def on_export(self, handler: ExportHandler) -> ExportHandler:
        self.export = handler
        return handler


class SubjectRoutes:
    """The routes igni calls, wired to the service's handlers."""

    def __init__(
        self, handlers: SubjectHandlers, directory: DirectoryClient, igni_caller: IgniCaller
    ) -> None:
        self._handlers = handlers
        self._directory = directory
        self._igni_caller = igni_caller

    async def subject(self, user_id: str, org_id: str) -> Subject:
        """The person, with their address and name when the directory can say."""
        try:
            member = await self._directory.person(org_id, user_id)
        except IgniUnavailable as exc:
            logger.warning("igni: could not look up the subject (%s); going by id alone", exc)
            member = None
        return Subject(
            user_id=user_id,
            org_id=org_id,
            email=member.email if member else "",
            name=member.name if member else "",
            member=member,
        )

    def router(self) -> APIRouter:
        # `Depends` as a default, not inside `Annotated`: these annotations
        # are strings (PEP 563), and FastAPI cannot resolve a string that
        # names a local variable such as `caller`.
        router = APIRouter(prefix=IgniPaths.SUBJECTS, tags=["igni"])
        handlers = self._handlers
        caller = self._igni_caller

        @router.delete(
            "/{user_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Erase one person's data in the calling organisation",
        )
        async def erase_subject(user_id: str, call: IgniCall = Depends(caller)) -> Response:
            if handlers.erase is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="This service has no erasure handler.",
                )
            await handlers.erase(await self.subject(user_id, call.org_id))
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @router.get(
            "/{user_id}/export",
            response_model=SubjectExport,
            summary="What this service holds about one person in the calling organisation",
        )
        async def export_subject(user_id: str, call: IgniCall = Depends(caller)) -> SubjectExport:
            if handlers.export is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="This service has no export handler.",
                )
            sections = await handlers.export(await self.subject(user_id, call.org_id))
            return SubjectExport(sections=list(sections))

        return router
