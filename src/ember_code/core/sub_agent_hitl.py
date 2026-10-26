"""Bridge HITL approval between sub-agents and the user-facing TUI.

When the orchestrator's `spawn_agent` runs a specialist, the specialist's
tool calls run inside the parent agent's tool execution. Their
``RunPausedEvent``s never reach the backend's main run loop — Agno only
streams them out of the *current* `arun()` call, and the parent's tool
function eats them.

This coordinator gives them a path out: the sub-agent's stream handler
pushes the pending requirements here, the backend's main run loop polls
for them and forwards them to the FE as ordinary ``HITLRequest`` events,
and :meth:`HitlController.resolve_single` routes confirmation back into
the coordinator. The sub-agent's stream handler awaits the resolution,
then resumes the sub-agent via ``acontinue_run``.

Scope: per-Session. One instance lives on ``Session`` and is injected
into ``OrchestrateTools``. Threading: every shared field is touched only
from the asyncio event loop; no locks needed.

Durable when given an :class:`ApprovalJournal` (:meth:`attach_journal`;
the backend always does): each request is a row from the moment it is
pushed until it is answered. After a restart :meth:`restore` brings the
requests back with fresh events, so they are shown again and can be
answered. The ``spawn_agent`` call that was waiting on one died with the
backend, so nothing resumes on the answer — its parent run is surfaced
as interrupted, like any run a restart cut off — but the person is not
left with a question nobody can take.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.approvals.journal import ApprovalJournal, ApprovalOrigin, SpawnLink
from ember_code.core.paths import DEFAULT_DATA_DIR
from ember_code.core.profile import RuntimeProfile
from ember_code.core.tools.spawn_context import SpawnCalls, SpawnDeadline
from ember_code.protocol import messages as msg
from ember_code.protocol.agno_tool_formatter import default_registry

if TYPE_CHECKING:
    from agno.run.base import RunRequirement


class _PendingEntry(BaseModel):
    """One in-flight sub-agent HITL requirement.

    Rule 1 compliance — was a ``@dataclass`` before. Uses
    ``arbitrary_types_allowed`` because ``requirement`` is Agno's
    ``RunRequirement`` (dynamic) and ``event`` is an
    :class:`asyncio.Event` (not a Pydantic type). Mutable — the
    ``surfaced`` flag flips once and the event resolves once, both
    inside the coordinator's own methods.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    requirement: Any  # RunRequirement
    run_id: str
    # Chain of agents that produced this requirement, parent → leaf.
    # ``["architect"]`` for a tool the architect specialist requested;
    # ``["architect", "reviewer"]`` if the architect spawned a reviewer
    # that then asked for permission. Surfaced to the FE so the dialog
    # can show *which* specialist is asking, not just the tool name.
    agent_path: list[str] = Field(default_factory=list)
    surfaced: bool = False  # True once forwarded to FE via list_new_pending
    event: asyncio.Event = Field(default_factory=asyncio.Event)


class RestoredRun(BaseModel):
    """A sub-agent run read back after a restart, all its approvals answered."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    requirements: list[Any]
    link: SpawnLink | None = None


class SubAgentHITLCoordinator:
    """Per-session registry of sub-agent HITL requirements.

    See module docstring for the full flow.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        # Set whenever a NEW requirement arrives. Backend awaits this to
        # know when to forward to FE without busy-polling.
        self.new_arrival = asyncio.Event()
        #: ``(req_id, details)`` for questions already on screen whose
        #: explanation improved — relayed by the same drain as new ones.
        self._detail_updates: list[tuple[str, str]] = []
        #: Forwarders currently relaying requirements to a person. Zero
        #: means a requirement pushed now would wait for ever — headless
        #: ``-p``, CI — so a caller that needs a yes must refuse instead.
        self.listeners = 0
        self._journal: ApprovalJournal | None = None
        self._session_id: Callable[[], str] = lambda: ""
        #: Requests read back after a restart: nothing waits on them.
        self._restored: set[str] = set()
        #: Requests a stream handler is blocked on right now.
        self._awaited: set[str] = set()
        #: Finished calls' results by run, for the runs a spawn of theirs
        #: may yet wait under (:meth:`tool_finished`); bounded.
        self._turns: dict[str, dict[str, str]] = {}
        #: Runs with a spawn waiting under them: their results are written.
        self._linked_runs: set[str] = set()
        #: Spawn links a previous backend left with no approval waiting:
        #: their run was being continued when it died.
        self._interrupted: list[SpawnLink] = []

    # ── Durability ────────────────────────────────────────────────

    def attach_journal(self, journal: ApprovalJournal, session_id: Callable[[], str]) -> None:
        """Write requests through to ``journal`` from now on."""
        self._journal = journal
        self._session_id = session_id

    def restore(self, session_id: str) -> int:
        """Bring back what ``session_id`` left waiting; returns how many."""
        if self._journal is None:
            return 0
        restored = 0
        waiting = self._journal.waiting(session_id)
        asked = {s.run_id for s in waiting if s.origin is ApprovalOrigin.SUB_AGENT}
        self._interrupted = [
            link for link in self._journal.spawns(session_id) if link.run_id not in asked
        ]
        for stored in waiting:
            if stored.origin is not ApprovalOrigin.SUB_AGENT:
                continue
            if stored.request is not None and stored.request.details:
                stored.requirement.details = stored.request.details  # type: ignore[attr-defined]
            # Already "surfaced": it reaches a window by
            # :meth:`waiting_requests`, when one attaches.
            self._pending[stored.requirement_id] = _PendingEntry(
                requirement=stored.requirement,
                run_id=stored.run_id,
                agent_path=stored.agent_path,
                surfaced=True,
            )
            self._restored.add(stored.requirement_id)
            restored += 1
        return restored

    @property
    def waiting_count(self) -> int:
        """Sub-agent runs blocked on a person right now — each one's
        spawn is doing nothing but wait (``BackendServer.activity``)."""
        return sum(1 for req_id in self._awaited if not self._pending[req_id].event.is_set())

    def take_ready_restored(self) -> list[RestoredRun]:
        """Restored runs every one of whose approvals is now answered.

        Each is returned once, with its requirements as decided and the
        link the journal kept, for :class:`ColdSpawnResume` to continue.
        """
        by_run: dict[str, list[str]] = {}
        for req_id in self._restored:
            by_run.setdefault(self._pending[req_id].run_id, []).append(req_id)
        ready: list[RestoredRun] = []
        for run_id, req_ids in by_run.items():
            if not all(self._pending[r].event.is_set() for r in req_ids):
                continue
            requirements = [self._pending.pop(r).requirement for r in req_ids]
            self._restored.difference_update(req_ids)
            link = self._journal.spawn(run_id) if self._journal is not None else None
            ready.append(RestoredRun(run_id=run_id, requirements=requirements, link=link))
        return ready

    def take_interrupted(self) -> list[SpawnLink]:
        """Links whose answered run was still being continued when the
        previous backend died — each returned once."""
        interrupted, self._interrupted = self._interrupted, []
        return interrupted

    def forget_spawn(self, run_id: str) -> None:
        if self._journal is not None:
            self._journal.forget_spawn(run_id)

    def request_for(self, req_id: str) -> msg.HITLRequest | None:
        """The request a window is shown for ``req_id``."""
        entry = self._pending.get(req_id)
        if entry is None:
            return None
        req = entry.requirement
        tool_exec = getattr(req, "tool_execution", None)
        raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
        return msg.HITLRequest(
            requirement_id=req_id,
            tool_name=raw_name,
            friendly_name=default_registry().friendly_name(raw_name),
            tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
            agent_path=list(entry.agent_path),
            details=str(getattr(req, "details", "") or ""),
            details_pending=bool(getattr(req, "details_pending", False)),
        )

    def waiting_requests(self) -> list[msg.RunPaused]:
        """One ``RunPaused`` per sub-agent run with requests not yet
        answered — what a window attaching to the session is shown."""
        by_run: dict[str, list[msg.HITLRequest]] = {}
        for req_id, entry in self._pending.items():
            if entry.event.is_set():
                continue
            request = self.request_for(req_id)
            if request is not None:
                by_run.setdefault(entry.run_id, []).append(request)
        return [msg.RunPaused(run_id=run_id, requirements=reqs) for run_id, reqs in by_run.items()]

    def _trace_file(self, run_id: str, agent_path: list[str] | None) -> None:
        try:
            with open(
                Path(os.path.expanduser(f"{DEFAULT_DATA_DIR}/hitl_trace.log")), "a"
            ) as trace_f:
                trace_f.write(
                    f"{time.strftime('%H:%M:%S')} pid={os.getpid()} "
                    f"coord.push_requirement: coord_id={id(self)} run_id={run_id} path={agent_path}\n"
                )
        except Exception:
            pass

    def _write(self, req_id: str) -> None:
        if self._journal is None:
            return
        entry = self._pending[req_id]
        self._journal.record(
            requirement_id=req_id,
            session_id=self._session_id(),
            run_id=entry.run_id,
            origin=ApprovalOrigin.SUB_AGENT,
            requirement=entry.requirement,
            request=self.request_for(req_id),
        )

    #: Runs whose finished calls are remembered in memory at once.
    TURNS_KEPT = 64

    def tool_finished(self, run_id: str, tool_call_id: str, result: str) -> None:
        """A call of ``run_id`` finished (``core/hooks/turn_results.py``).

        Kept for the turn a spawn of that run may be waiting in; written
        to the journal once a spawn of the run waits on a person, so a
        restart can continue the turn with every call's result.
        """
        turn = self._turns.setdefault(run_id, {})
        turn[tool_call_id] = result
        while len(self._turns) > self.TURNS_KEPT:
            self._turns.pop(next(iter(self._turns)))
        if self._journal is not None and run_id in self._linked_runs:
            self._journal.record_turn_result(run_id, tool_call_id, result)

    def turn_results(self, run_id: str) -> dict[str, str]:
        return self._journal.turn_results(run_id) if self._journal is not None else {}

    def forget_turn(self, run_id: str) -> None:
        self._linked_runs.discard(run_id)
        self._turns.pop(run_id, None)
        if self._journal is not None:
            self._journal.forget_turn(run_id)

    def _link(self, run_id: str, session_id: str, agent_path: list[str]) -> None:
        parents = SpawnCalls.current()
        if self._journal is None or not parents or not session_id or not agent_path:
            return
        for parent in parents:
            if parent.run_id in self._linked_runs:
                continue
            self._linked_runs.add(parent.run_id)
            for call_id, result in self._turns.get(parent.run_id, {}).items():
                self._journal.record_turn_result(parent.run_id, call_id, result)
        self._journal.record_spawn(
            SpawnLink(
                run_id=run_id,
                session_id=self._session_id(),
                agent_session_id=session_id,
                agent_name=agent_path[-1],
                parents=parents,
            )
        )

    def _forget(self, req_id: str) -> None:
        if self._journal is not None:
            self._journal.forget(req_id)

    async def push_requirement(
        self,
        req: RunRequirement,
        run_id: str,
        agent_path: list[str] | None = None,
        session_id: str = "",
    ) -> str:
        """Called from the sub-agent stream handler when its run pauses.

        ``session_id`` is the sub-agent run's own Agno session: with the
        calls waiting above it (:class:`SpawnCalls`) it is what a backend
        that starts cold needs to continue the run.
        """
        logging.getLogger("ember_code.llm_calls").info(
            "coord.push_requirement: id_self=%s run_id=%s path=%s",
            id(self),
            run_id,
            agent_path,
        )
        # Direct trace — see backend/server.py for why we bypass logging.
        # The cloud keeps no trace file: the record goes to standard output.
        if RuntimeProfile.current().is_cloud:
            logging.getLogger("ember_code.hitl_trace").info(
                "coord.push_requirement: coord_id=%s run_id=%s path=%s",
                id(self),
                run_id,
                agent_path,
            )
        else:
            self._trace_file(run_id, agent_path)
        req_id = str(uuid.uuid4())[:8]
        self._pending[req_id] = _PendingEntry(
            requirement=req,
            run_id=run_id,
            agent_path=list(agent_path or []),
        )
        self._write(req_id)
        self._link(run_id, session_id, list(agent_path or []))
        self.new_arrival.set()
        return req_id

    def list_new_pending(self) -> list[tuple[str, _PendingEntry]]:
        """Return entries not yet surfaced to FE; mark them as surfaced.

        Called by the backend run loop. Idempotent across calls — once
        an entry is surfaced it won't be returned again.
        """
        out: list[tuple[str, _PendingEntry]] = []
        for req_id, entry in self._pending.items():
            if not entry.surfaced and not entry.event.is_set():
                entry.surfaced = True
                out.append((req_id, entry))
        # The arrival event is one-shot — caller resets after consuming.
        self.new_arrival.clear()
        return out

    def update_details(self, req_id: str, details: str) -> None:
        """Set why a pending question is being asked, on screen too.

        Not yet surfaced: it goes out with the new text. Already
        surfaced: queued for the relay to send as an update. Answered or
        gone: nothing to do.
        """
        entry = self._pending.get(req_id)
        if entry is None or entry.event.is_set():
            return
        entry.requirement.details = details
        if hasattr(entry.requirement, "details_pending"):
            entry.requirement.details_pending = False
        if self._journal is not None:
            request = self.request_for(req_id)
            if request is not None:
                self._journal.update_request(req_id, request)
        if entry.surfaced:
            self._detail_updates.append((req_id, details))
            self.new_arrival.set()

    def take_detail_updates(self) -> list[tuple[str, str]]:
        """Updates for still-open questions; each is returned once."""
        updates = [
            (req_id, details)
            for req_id, details in self._detail_updates
            if req_id in self._pending and not self._pending[req_id].event.is_set()
        ]
        self._detail_updates.clear()
        return updates

    def has_unresolved(self) -> bool:
        return any(not e.event.is_set() for e in self._pending.values())

    def resolve(self, req_id: str, action: str) -> bool:
        """Called from :meth:`HitlController.resolve_batch`. Returns
        True if handled here."""
        entry = self._pending.get(req_id)
        if entry is None:
            return False
        if action == "confirm":
            entry.requirement.confirm()
        else:
            entry.requirement.reject(note="User denied")
        entry.event.set()
        # Answered: a restart from here on must not ask again. A
        # restored request stays until its run is continued
        # (:meth:`take_ready_restored`): nothing in this process waits
        # on it.
        self._forget(req_id)
        return True

    async def wait_resolved(self, req_id: str) -> RunRequirement:
        """Called from the sub-agent stream handler. Blocks until user resolves."""
        entry = self._pending[req_id]
        self._awaited.add(req_id)
        try:
            # However long the person takes: approvals do not expire.
            with SpawnDeadline.waiting_on_person():
                await entry.event.wait()
        finally:
            self._awaited.discard(req_id)
        return entry.requirement

    def cleanup(self, req_id: str) -> None:
        """Drop the entry once spawn_agent has resumed past it."""
        self._pending.pop(req_id, None)
        # The spawn link stays until the run's stream ends: a restart
        # while the answered run is still being continued must find it
        # (:meth:`take_interrupted`).
        self._forget(req_id)
