"""Approvals waiting on the person, written as they happen.

An approval used to live only in the backend's memory — the main
agent's paused requirements in ``PendingRequirementsStore``, a
sub-agent's in ``SubAgentHITLCoordinator`` — so a backend that
restarted while a dialog was open lost the question: the window kept
showing it, and answering it said "Unknown requirement". In the cloud a
producer's VM is a cache that can be lost at any time (``docs/CLOUD.md``
section 9), which makes that the common case rather than a rare one.

:class:`ApprovalJournal` keeps one row per approval in the project's
database (the desktop's ``state.db``, the shared PostgreSQL database in
the cloud, scoped like every other row): the Agno requirement as Agno
serialises it, and the request the window was shown. A row is written
when the approval is raised and deleted when it is answered or its run
ends; a backend that starts reads them back, so the approval is shown
again and can be answered.

The requirements the permission policy decided by itself while others
in the same pause waited on the person (``auto_resolved``) are kept
too: the run cannot be resumed without them.

A sub-agent's approval has more to keep: the agent waiting on it runs
inside a ``spawn_agent`` call of another run. ``waiting_spawns`` holds,
per paused sub-agent run, which agent it is and the chain of calls
waiting above it (:class:`~ember_code.core.tools.spawn_context.ParentCall`)
— what a backend that starts cold needs to continue the sub-agent, hand
its answer to the waiting call and continue the run that made it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from agno.run.requirement import RunRequirement
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Index, Integer, PrimaryKeyConstraint, String, Text, delete, select, update
from sqlalchemy.orm import Mapped, mapped_column

from ember_code.core.approvals.events import (
    ApprovalChange,
    ApprovalState,
    approval_events,
)
from ember_code.core.db.base import Base
from ember_code.core.db.database import Database
from ember_code.core.db.scope import ScopedColumns
from ember_code.core.tools.spawn_context import ParentCall
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class ApprovalOrigin(StrEnum):
    #: The main agent's run paused on it.
    MAIN = "main"
    #: A sub-agent's run, inside a ``spawn_agent`` call, paused on it.
    SUB_AGENT = "sub_agent"
    #: Decided by the permission policy; kept for the resume of its run.
    AUTO_RESOLVED = "auto_resolved"


class WaitingApprovalModel(ScopedColumns, Base):
    __tablename__ = "waiting_approvals"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "requirement_id", name="pk_waiting_approvals"
        ),
        Index("ix_waiting_approvals_session", "org", "producer", "project", "session_id"),
    )

    requirement_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    origin: Mapped[str] = mapped_column(String, nullable=False)
    #: ``RunRequirement.to_dict()`` as JSON.
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    #: The :class:`msg.HITLRequest` the window was shown, as JSON; empty
    #: for an auto-resolved requirement, which nobody is shown.
    request: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WaitingSpawnModel(ScopedColumns, Base):
    """A paused sub-agent run, and the calls waiting above it."""

    __tablename__ = "waiting_spawns"
    __table_args__ = (
        PrimaryKeyConstraint("org", "producer", "project", "run_id", name="pk_waiting_spawns"),
    )

    #: The sub-agent's paused run.
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The igni session the approval belongs to.
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The sub-agent run's own Agno session.
    agent_session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    #: ``list[ParentCall]`` as JSON, nearest first.
    parents: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class TurnResultModel(ScopedColumns, Base):
    """A finished call's result, in a turn whose spawn waits on a person."""

    __tablename__ = "turn_results"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org", "producer", "project", "run_id", "tool_call_id", name="pk_turn_results"
        ),
    )

    run_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SpawnLink(BaseModel):
    """What continuing a paused sub-agent run cold needs."""

    run_id: str
    session_id: str
    agent_session_id: str
    agent_name: str
    parents: list[ParentCall] = Field(default_factory=list)


class StoredApproval(BaseModel):
    """One approval read back from the journal."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    requirement_id: str
    session_id: str
    run_id: str
    origin: ApprovalOrigin
    requirement: RunRequirement
    request: msg.HITLRequest | None = None
    agent_path: list[str] = Field(default_factory=list)


class ApprovalJournal:
    """The ``waiting_approvals`` table of one project's database.

    Every write is best-effort: a failed one is logged and the approval
    still works in memory — it just would not survive a restart. A
    requirement that is not Agno's (a test double) is not written.
    """

    def __init__(self, db: Database | str | Path) -> None:
        self._db = Database.of(db)

    def record(
        self,
        *,
        requirement_id: str,
        session_id: str,
        run_id: str | None,
        origin: ApprovalOrigin,
        requirement: Any,
        request: msg.HITLRequest | None = None,
    ) -> None:
        if not isinstance(requirement, RunRequirement):
            return
        try:
            serialised = json.dumps(requirement.to_dict(), default=str)
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(WaitingApprovalModel).where(
                        self._db.scope.where(WaitingApprovalModel),
                        WaitingApprovalModel.requirement_id == requirement_id,
                    )
                )
                session.add(
                    WaitingApprovalModel(
                        **self._db.scope.stamp(),
                        requirement_id=requirement_id,
                        session_id=session_id,
                        run_id=run_id or "",
                        origin=origin.value,
                        requirement=serialised,
                        request=request.model_dump_json() if request is not None else "",
                        created_at=int(time.time()),
                    )
                )
        except Exception as exc:
            logger.debug("approval journal: record %s failed: %s", requirement_id, exc)
        if origin is not ApprovalOrigin.AUTO_RESOLVED:
            approval_events.announce(
                ApprovalChange(
                    session_id=session_id,
                    requirement_id=requirement_id,
                    run_id=run_id or "",
                    state=ApprovalState.WAITING,
                    origin=origin.value,
                    tool_name=request.tool_name if request is not None else "",
                )
            )

    def record_auto_resolved(self, *, session_id: str, run_id: str, requirement: Any) -> None:
        self.record(
            requirement_id=f"auto-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            run_id=run_id,
            origin=ApprovalOrigin.AUTO_RESOLVED,
            requirement=requirement,
        )

    def update_request(self, requirement_id: str, request: msg.HITLRequest) -> None:
        """What the window is shown changed (a better explanation)."""
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    update(WaitingApprovalModel)
                    .where(
                        self._db.scope.where(WaitingApprovalModel),
                        WaitingApprovalModel.requirement_id == requirement_id,
                    )
                    .values(request=request.model_dump_json())
                )
        except Exception as exc:
            logger.debug("approval journal: update %s failed: %s", requirement_id, exc)

    def forget(self, requirement_id: str) -> None:
        """Answered: the approval is no longer waiting."""
        self._delete(WaitingApprovalModel.requirement_id == requirement_id)

    def forget_run(self, run_id: str, *, origin: ApprovalOrigin | None = None) -> None:
        """The run ended or resumed: nothing of it is waiting any more."""
        if origin is None:
            self._delete(WaitingApprovalModel.run_id == run_id)
        else:
            self._delete(
                WaitingApprovalModel.run_id == run_id,
                WaitingApprovalModel.origin == origin.value,
            )

    def record_spawn(self, link: SpawnLink) -> None:
        """Remember a paused sub-agent run (once per run)."""
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(WaitingSpawnModel).where(
                        self._db.scope.where(WaitingSpawnModel),
                        WaitingSpawnModel.run_id == link.run_id,
                    )
                )
                session.add(
                    WaitingSpawnModel(
                        **self._db.scope.stamp(),
                        run_id=link.run_id,
                        session_id=link.session_id,
                        agent_session_id=link.agent_session_id,
                        agent_name=link.agent_name,
                        parents=json.dumps([p.model_dump(mode="json") for p in link.parents]),
                        created_at=int(time.time()),
                    )
                )
        except Exception as exc:
            logger.debug("approval journal: record spawn %s failed: %s", link.run_id, exc)

    def record_turn_result(self, run_id: str, tool_call_id: str, result: str) -> None:
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(TurnResultModel).where(
                        self._db.scope.where(TurnResultModel),
                        TurnResultModel.run_id == run_id,
                        TurnResultModel.tool_call_id == tool_call_id,
                    )
                )
                session.add(
                    TurnResultModel(
                        **self._db.scope.stamp(),
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                        result=result,
                        created_at=int(time.time()),
                    )
                )
        except Exception as exc:
            logger.debug("approval journal: turn result %s failed: %s", tool_call_id, exc)

    def turn_results(self, run_id: str) -> dict[str, str]:
        """``tool_call_id → result`` of the calls of ``run_id`` that finished."""
        try:
            with self._db.sync_session() as session:
                rows = session.execute(
                    select(TurnResultModel.tool_call_id, TurnResultModel.result).where(
                        self._db.scope.where(TurnResultModel), TurnResultModel.run_id == run_id
                    )
                ).all()
        except Exception as exc:
            logger.debug("approval journal: read turn %s failed: %s", run_id, exc)
            return {}
        return {call_id: result for call_id, result in rows}

    def forget_turn(self, run_id: str) -> None:
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(TurnResultModel).where(
                        self._db.scope.where(TurnResultModel), TurnResultModel.run_id == run_id
                    )
                )
        except Exception as exc:
            logger.debug("approval journal: forget turn %s failed: %s", run_id, exc)

    def spawn(self, run_id: str) -> SpawnLink | None:
        try:
            with self._db.sync_session() as session:
                row = session.scalar(
                    select(WaitingSpawnModel).where(
                        self._db.scope.where(WaitingSpawnModel),
                        WaitingSpawnModel.run_id == run_id,
                    )
                )
            if row is None:
                return None
            return SpawnLink(
                run_id=row.run_id,
                session_id=row.session_id,
                agent_session_id=row.agent_session_id,
                agent_name=row.agent_name,
                parents=[ParentCall.model_validate(p) for p in json.loads(row.parents)],
            )
        except Exception as exc:
            logger.debug("approval journal: read spawn %s failed: %s", run_id, exc)
            return None

    def spawns(self, session_id: str) -> list[SpawnLink]:
        """Every paused sub-agent run of the session that something waits on."""
        try:
            with self._db.sync_session() as session:
                run_ids = session.scalars(
                    select(WaitingSpawnModel.run_id).where(
                        self._db.scope.where(WaitingSpawnModel),
                        WaitingSpawnModel.session_id == session_id,
                    )
                ).all()
        except Exception as exc:
            logger.debug("approval journal: read spawns of %s failed: %s", session_id, exc)
            return []
        return [link for run_id in run_ids if (link := self.spawn(run_id)) is not None]

    def forget_spawn(self, run_id: str) -> None:
        try:
            with self._db.sync_session() as session, session.begin():
                session.execute(
                    delete(WaitingSpawnModel).where(
                        self._db.scope.where(WaitingSpawnModel),
                        WaitingSpawnModel.run_id == run_id,
                    )
                )
        except Exception as exc:
            logger.debug("approval journal: forget spawn %s failed: %s", run_id, exc)

    def waiting(self, session_id: str) -> list[StoredApproval]:
        """Everything the session's approvals were, oldest first."""
        try:
            with self._db.sync_session() as session:
                rows = session.scalars(
                    select(WaitingApprovalModel)
                    .where(
                        self._db.scope.where(WaitingApprovalModel),
                        WaitingApprovalModel.session_id == session_id,
                    )
                    .order_by(WaitingApprovalModel.created_at, WaitingApprovalModel.requirement_id)
                ).all()
        except Exception as exc:
            logger.debug("approval journal: read for %s failed: %s", session_id, exc)
            return []
        return [stored for row in rows if (stored := self._stored(row)) is not None]

    def _delete(self, *conditions: Any) -> None:
        mine = (self._db.scope.where(WaitingApprovalModel), *conditions)
        try:
            with self._db.sync_session() as session, session.begin():
                gone = session.execute(
                    select(
                        WaitingApprovalModel.requirement_id,
                        WaitingApprovalModel.session_id,
                        WaitingApprovalModel.run_id,
                        WaitingApprovalModel.origin,
                    ).where(*mine)
                ).all()
                session.execute(delete(WaitingApprovalModel).where(*mine))
        except Exception as exc:
            logger.debug("approval journal: delete failed: %s", exc)
            return
        for requirement_id, session_id, run_id, origin in gone:
            if origin == ApprovalOrigin.AUTO_RESOLVED.value:
                continue
            approval_events.announce(
                ApprovalChange(
                    session_id=session_id,
                    requirement_id=requirement_id,
                    run_id=run_id,
                    state=ApprovalState.RESOLVED,
                    origin=origin,
                )
            )

    @staticmethod
    def _stored(row: WaitingApprovalModel) -> StoredApproval | None:
        try:
            requirement = RunRequirement.from_dict(json.loads(row.requirement))
            request = msg.HITLRequest.model_validate_json(row.request) if row.request else None
            return StoredApproval(
                requirement_id=row.requirement_id,
                session_id=row.session_id,
                run_id=row.run_id,
                origin=ApprovalOrigin(row.origin),
                requirement=requirement,
                request=request,
                agent_path=list(request.agent_path) if request is not None else [],
            )
        except Exception as exc:
            logger.debug("approval journal: unreadable row %s: %s", row.requirement_id, exc)
            return None


__all__ = [
    "ApprovalJournal",
    "ApprovalOrigin",
    "SpawnLink",
    "StoredApproval",
    "TurnResultModel",
    "WaitingApprovalModel",
    "WaitingSpawnModel",
]
