"""Bridge HITL approval between sub-agents and the user-facing TUI.

When the orchestrator's `spawn_agent` runs a specialist, the specialist's
tool calls run inside the parent agent's tool execution. Their
``RunPausedEvent``s never reach the backend's main run loop — Agno only
streams them out of the *current* `arun()` call, and the parent's tool
function eats them.

This coordinator gives them a path out: the sub-agent's stream handler
pushes the pending requirements here, the backend's main run loop polls
for them and forwards them to the FE as ordinary ``HITLRequest`` events,
and ``resolve_hitl`` routes confirmation back into the coordinator. The
sub-agent's stream handler awaits the resolution, then resumes the
sub-agent via ``acontinue_run``.

Scope: per-Session. One instance lives on ``Session`` and is injected
into ``OrchestrateTools``. Threading: every shared field is touched only
from the asyncio event loop; no locks needed.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agno.run.base import RunRequirement


@dataclass
class _PendingEntry:
    requirement: Any  # RunRequirement
    run_id: str
    # Chain of agents that produced this requirement, parent → leaf.
    # ``["architect"]`` for a tool the architect specialist requested;
    # ``["architect", "reviewer"]`` if the architect spawned a reviewer
    # that then asked for permission. Surfaced to the FE so the dialog
    # can show *which* specialist is asking, not just the tool name.
    agent_path: list[str] = field(default_factory=list)
    surfaced: bool = False  # True once forwarded to FE via list_new_pending
    event: asyncio.Event = field(default_factory=asyncio.Event)


class SubAgentHITLCoordinator:
    """Per-session registry of sub-agent HITL requirements.

    See module docstring for the full flow.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        # Set whenever a NEW requirement arrives. Backend awaits this to
        # know when to forward to FE without busy-polling.
        self.new_arrival = asyncio.Event()

    async def push_requirement(
        self,
        req: RunRequirement,
        run_id: str,
        agent_path: list[str] | None = None,
    ) -> str:
        """Called from the sub-agent stream handler when its run pauses."""
        import logging as _log
        import os as _os
        import time as _t
        from pathlib import Path as _Path

        _log.getLogger("ember_code.llm_calls").info(
            "coord.push_requirement: id_self=%s run_id=%s path=%s",
            id(self),
            run_id,
            agent_path,
        )
        # Direct trace — see backend/server.py for why we bypass logging.
        try:
            with open(_Path(_os.path.expanduser("~/.ember/hitl_trace.log")), "a") as _f:
                _f.write(
                    f"{_t.strftime('%H:%M:%S')} pid={_os.getpid()} "
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

    def has_unresolved(self) -> bool:
        return any(not e.event.is_set() for e in self._pending.values())

    def resolve(self, req_id: str, action: str) -> bool:
        """Called from backend.resolve_hitl. Returns True if handled here."""
        entry = self._pending.get(req_id)
        if entry is None:
            return False
        if action == "confirm":
            entry.requirement.confirm()
        else:
            entry.requirement.reject(note="User denied")
        entry.event.set()
        return True

    async def wait_resolved(self, req_id: str) -> RunRequirement:
        """Called from the sub-agent stream handler. Blocks until user resolves."""
        entry = self._pending[req_id]
        await entry.event.wait()
        return entry.requirement

    def cleanup(self, req_id: str) -> None:
        """Drop the entry once spawn_agent has resumed past it."""
        self._pending.pop(req_id, None)
