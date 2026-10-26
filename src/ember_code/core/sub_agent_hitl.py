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

from ember_code.core.paths import DEFAULT_DATA_DIR
from ember_code.core.session.approval_journal import ApprovalJournal, ApprovalOrigin
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
        for stored in self._journal.waiting(session_id):
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

    def _forget(self, req_id: str) -> None:
        if self._journal is not None:
            self._journal.forget(req_id)

    async def push_requirement(
        self,
        req: RunRequirement,
        run_id: str,
        agent_path: list[str] | None = None,
    ) -> str:
        """Called from the sub-agent stream handler when its run pauses."""
        logging.getLogger("ember_code.llm_calls").info(
            "coord.push_requirement: id_self=%s run_id=%s path=%s",
            id(self),
            run_id,
            agent_path,
        )
        # Direct trace — see backend/server.py for why we bypass logging.
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
        req_id = str(uuid.uuid4())[:8]
        self._pending[req_id] = _PendingEntry(
            requirement=req,
            run_id=run_id,
            agent_path=list(agent_path or []),
        )
        self._write(req_id)
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
        # Answered: a restart from here on must not ask again.
        self._forget(req_id)
        if req_id in self._restored:
            # Nothing waits on a restored request to clean it up.
            self._restored.discard(req_id)
            self._pending.pop(req_id, None)
        return True

    async def wait_resolved(self, req_id: str) -> RunRequirement:
        """Called from the sub-agent stream handler. Blocks until user resolves."""
        entry = self._pending[req_id]
        await entry.event.wait()
        return entry.requirement

    def cleanup(self, req_id: str) -> None:
        """Drop the entry once spawn_agent has resumed past it."""
        self._pending.pop(req_id, None)
        self._forget(req_id)
