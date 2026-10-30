"""Whose rows: the org, the producer and the project every row belongs to.

``docs/CLOUD.md`` section 9. In the cloud one PostgreSQL database holds
an org's state, shared by its people and apps; the org is the tenant, the
producer and the project are filters. Every igni-owned table carries the
three columns (:class:`ScopedColumns`), and every query a store makes
goes through a :class:`StorageScope` — which is why it is the one place
to add PostgreSQL row-level security when that is decided: set the
scope per connection here, and a filter a store forgot returns nothing
instead of somebody else's rows.

On the desktop nothing is shared. A project's ``state.db`` is already
its own file, so :meth:`StorageScope.local` is the empty scope: every
column holds ``""``, the filters still run, and they match exactly the
rows a desktop has always had. The real values exist only where several
producers share one database (``IGNI_DATABASE_URL`` set): the producer
and org come from the signed-in principal (:class:`Tenancy`), the
project from the session's folder.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, String, and_
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.data_paths import resolve_project_id


class ScopedColumns:
    """The three columns every igni-owned table carries.

    ``""`` where nothing is shared — the desktop — so a desktop row and
    the empty scope's filter always agree.
    """

    org: Mapped[str] = mapped_column(String, nullable=False, server_default="", default="")
    producer: Mapped[str] = mapped_column(String, nullable=False, server_default="", default="")
    project: Mapped[str] = mapped_column(String, nullable=False, server_default="", default="")


class Tenant(BaseModel):
    """Who the rows are for, where several producers share a database."""

    model_config = ConfigDict(frozen=True)

    #: The organisation — the tenant.
    org: str
    #: ``person:<id>`` or ``app:<id>`` (:attr:`Principal.ref`).
    producer: str


class NoTenantError(RuntimeError):
    """A shared database was opened with nobody to scope its rows to."""


class StorageScope(BaseModel):
    """The org, producer and project a store reads and writes.

    Stores never compare the columns themselves: they ask for
    :meth:`where` (or :meth:`tenant_where`) and :meth:`stamp`, so the
    filter lives here and only here.
    """

    model_config = ConfigDict(frozen=True)

    org: str = ""
    producer: str = ""
    project: str = ""
    #: Several producers share the database. ``False`` on the desktop.
    shared: bool = False

    @classmethod
    def local(cls) -> StorageScope:
        """The desktop's scope: one person, one file per project."""
        return cls()

    @classmethod
    def for_tenant(cls, tenant: Tenant, project: str = "") -> StorageScope:
        return cls(org=tenant.org, producer=tenant.producer, project=project, shared=True)

    def for_project(self, project_dir: str | Path | None) -> StorageScope:
        """This scope, narrowed to ``project_dir``'s project.

        The project is :func:`resolve_project_id` — the same id that
        names the desktop's per-project directory, so two clones of one
        repository share their state in the cloud as they do at home.
        The desktop's scope has no project column to fill: its file is
        the project.
        """
        if not self.shared or project_dir is None:
            return self
        return self.model_copy(update={"project": resolve_project_id(project_dir)})

    def without_project(self) -> StorageScope:
        """For what belongs to the producer rather than to one folder."""
        return self.model_copy(update={"project": ""})

    def where(self, model: Any) -> ColumnElement[bool]:
        """The filter for ``model``'s rows in this scope: org, producer, project."""
        return and_(
            model.org == self.org,
            model.producer == self.producer,
            model.project == self.project,
        )

    def tenant_where(self, model: Any) -> ColumnElement[bool]:
        """Org and producer only — for a lookup asked before the project is known.

        Which folder a session belongs to is that question: it is asked
        to find the project, so it cannot filter by it.
        """
        return and_(model.org == self.org, model.producer == self.producer)

    def stamp(self) -> dict[str, str]:
        """The scope columns of a row written in this scope."""
        return {"org": self.org, "producer": self.producer, "project": self.project}

    @property
    def agno_user_id(self) -> str:
        """Agno's tables have no scope columns; its ``user_id`` carries the scope.

        ``<org>/<producer>/<project>`` — sessions, memories and learnings
        Agno writes for this scope are found again by it.
        """
        return f"{self.org}/{self.producer}/{self.project}"


class Tenancy:
    """Where a shared database's scope comes from.

    The backend says, once, how to ask who is signed in (:meth:`use`);
    a store opening a shared database asks (:meth:`scope`). Read on each
    open rather than remembered, as the principal itself is: signing in
    and out changes the answer. On the desktop nothing is asked.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source: Callable[[], Tenant | None] | None = None

    def use(self, source: Callable[[], Tenant | None] | None) -> None:
        with self._lock:
            self._source = source

    def tenant(self) -> Tenant | None:
        with self._lock:
            source = self._source
        return source() if source is not None else None

    def scope(self, *, shared: bool) -> StorageScope:
        """The scope to open a database in, without a project yet.

        Raises :class:`NoTenantError` for a shared database nobody is
        signed in to: an unscoped read there would be every producer's
        rows.
        """
        if not shared:
            return StorageScope.local()
        tenant = self.tenant()
        if tenant is None:
            raise NoTenantError(
                "IGNI_DATABASE_URL names a shared database, and nobody is signed in "
                "to scope its rows to"
            )
        return StorageScope.for_tenant(tenant)


#: The process's tenancy: a cloud backend serves one producer.
tenancy = Tenancy()


__all__ = [
    "NoTenantError",
    "ScopedColumns",
    "StorageScope",
    "Tenancy",
    "Tenant",
    "tenancy",
]
