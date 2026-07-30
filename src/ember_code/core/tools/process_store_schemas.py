"""Pydantic wire/data models for the background-process store.

Extracted from :mod:`process_store` per the sibling schemas
convention (mirrors :mod:`shell_orphan_schemas`'s
"promoted to Pydantic (Rule 1)" note verbatim). Every wire /
result model this subsystem hands across a module boundary lives
here so Rule 1 stays discoverable at one path.

Consumers:

* :class:`BackgroundProcessRow` — in-memory shape of a persisted
  background-process row. Formerly a frozen ``@dataclass`` —
  promoted to Pydantic (Rule 1) so validation catches a caller
  handing in a str pid or a float ``started_at``. Owns
  :meth:`BackgroundProcessRow.new` (stamps its own
  ``started_at`` via ``time.time()`` so callers don't need a
  module-level ``now_epoch`` helper), :meth:`from_model` (mapping
  from the ORM row — kept duck-typed to avoid an import cycle
  with :mod:`process_store`), and :meth:`to_upsert_values` (the
  values-dict for the SQLite upsert).
* :class:`UpsertResult` / :class:`RemoveResult` /
  :class:`ListResult` — typed replacements for the previous
  ``None`` returns of :meth:`BackgroundProcessStore.upsert` /
  :meth:`remove` / :meth:`list_all`. DB failure at the store
  boundary now becomes a typed ``reason`` string instead of an
  exception the caller has to guess about — mirrors
  :class:`RehydrateResult`'s design in
  :mod:`shell_orphan_schemas`.

TODO(refactor): once ``LoopStore`` and ``scheduler.store`` grow
their own resolver classes, extract a shared ``DbPathResolver``
consumed by all three stores. Left as a follow-up so this PR
stays scoped to the process-store subsystem.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Duck-typed at runtime — the mapping only reads .pid / .cmd /
    # .pgid / .started_at. Importing the ORM class here would
    # cycle with :mod:`process_store` which imports this module.
    pass


class BackgroundProcessRow(BaseModel):
    """In-memory shape of a persisted background-process row.

    Formerly a frozen ``@dataclass`` — promoted to Pydantic
    (Rule 1) so validation catches a caller handing in a str pid,
    a float ``started_at``, etc.

    ``model_config = {"frozen": True}`` preserves the original
    dataclass's immutability guarantee so the store still hands
    out immutable snapshots.
    """

    model_config = {"frozen": True}

    pid: int
    cmd: str
    pgid: int | None = None
    started_at: int  # epoch seconds

    @classmethod
    def new(
        cls,
        pid: int,
        cmd: str,
        pgid: int | None,
        *,
        now: int | None = None,
    ) -> BackgroundProcessRow:
        """Construct a fresh row with ``started_at`` stamped to
        the current epoch second.

        Absorbs the former module-level ``now_epoch()`` helper —
        the timestamp policy lives on the model that owns the
        field, not on a sibling free function. Tests that need a
        deterministic clock pass ``now=`` explicitly instead of
        monkeypatching a global.
        """
        stamped = now if now is not None else int(time.time())
        return cls(pid=pid, cmd=cmd, pgid=pgid, started_at=stamped)

    @classmethod
    def from_model(cls, m: Any) -> BackgroundProcessRow:
        """Build a row from a duck-typed ORM object exposing
        ``.pid`` / ``.cmd`` / ``.pgid`` / ``.started_at``.

        Kept duck-typed (no ``BackgroundProcessModel`` import) to
        avoid an import cycle with :mod:`process_store`, which
        imports this schemas module. Absorbs the list-
        comprehension mapping that used to live inline in
        :meth:`BackgroundProcessStore.list_all`.
        """
        return cls(
            pid=m.pid,
            cmd=m.cmd,
            pgid=m.pgid,
            started_at=m.started_at,
        )

    def to_upsert_values(self) -> dict[str, int | str | None]:
        """Return the ``values()`` dict for the SQLite upsert.

        Absorbs the hand-rolled ``insert().values(pid=..., cmd=...,
        ...)`` shape that used to live inline in
        :meth:`BackgroundProcessStore.upsert`. Keeping the field
        mapping on the model means adding a column is a one-file
        edit instead of a grep-and-update across the store's
        insert / update / list paths.
        """
        return {
            "pid": self.pid,
            "cmd": self.cmd,
            "pgid": self.pgid,
            "started_at": self.started_at,
        }


class UpsertResult(BaseModel):
    """Typed outcome of :meth:`BackgroundProcessStore.upsert`.

    Callers used to swallow SQLAlchemy exceptions at the fire-
    and-forget boundary (:meth:`ProcessRegistry._persist_add`)
    without any signal a row failed to land. Wrapping the DB
    call in a typed result lets the scheduler log the reason at
    DEBUG without adding an ``except Exception`` at every caller.
    """

    ok: bool = True
    reason: str = Field(default="")


class RemoveResult(BaseModel):
    """Typed outcome of :meth:`BackgroundProcessStore.remove`.

    * ``ok`` — ``True`` iff the delete completed without an
      unhandled DB exception. Removing an absent pid is still
      ``ok=True`` — ``removed=False`` signals "nothing to do".
    * ``removed`` — ``True`` iff the delete affected a row. Lets
      :class:`OrphanRehydrator` count "actually pruned" rows
      distinctly from "no-op idempotent removes".
    * ``reason`` — short label + exception message on failure.
    """

    ok: bool = True
    removed: bool = False
    reason: str = Field(default="")


class ListResult(BaseModel):
    """Typed outcome of :meth:`BackgroundProcessStore.list_all`.

    Replaces the raw ``list[BackgroundProcessRow]`` return so a DB
    failure at ``list_all`` becomes an observable ``ok=False`` +
    ``reason`` payload instead of a raised exception the caller
    (:meth:`OrphanRehydrator.run`) has to wrap in its own try/
    except. Mirrors :class:`RehydrateResult`'s design.
    """

    ok: bool = True
    rows: list[BackgroundProcessRow] = Field(default_factory=list)
    reason: str = Field(default="")
