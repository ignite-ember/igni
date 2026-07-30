"""End-to-end test for the sub-agent HITL plumbing.

Bug-fix repro: when ``spawn_agent`` runs a specialist, the specialist's
``RunPausedEvent``s used to be silently dropped — its tool calls returned
empty results because the user never got asked. The fix is the
``SubAgentHITLCoordinator`` (core/sub_agent_hitl.py) bridging the
sub-agent's stream and the backend run loop.

Test strategy: mock Agno's Agent with a tiny fake that emits a
controlled stream (RunStarted → RunPaused → ... post-resume → RunContent →
RunCompleted). Drive ``_run_agent_streaming`` from
``core.tools.orchestrate`` and assert the coordinator receives the
requirement, the resume call lands, and the final content is captured.

Deterministic — no LLM, no network. Fast (<1s).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunPausedEvent,
    RunStartedEvent,
)
from pydantic import BaseModel

from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator, _PendingEntry
from ember_code.core.tools.orchestrate import _run_agent_streaming


class _FakeRequirement:
    """Minimal Agno-like RunRequirement for the test fake."""

    def __init__(self) -> None:
        self.confirmed = False
        self.rejected = False
        self.reject_note: str | None = None

    def is_resolved(self) -> bool:
        return self.confirmed or self.rejected

    def confirm(self) -> None:
        self.confirmed = True

    def reject(self, note: str = "") -> None:
        self.rejected = True
        self.reject_note = note


class _FakeRunOutput:
    """Stand-in for ``agno.run.RunOutput``. The orchestrate code reads the
    final response from ``agent.run_response.content`` (Agno's canonical
    location), so the fake exposes the same attribute."""

    def __init__(self, content: str = "") -> None:
        self.content = content


class _FakeAgent:
    """Mimics ``agno.agent.Agent``'s pause/resume surface.

    First ``arun()`` yields RunStartedEvent then RunPausedEvent (with a
    requirement). Then waits for ``acontinue_run()`` — which yields a
    RunContentEvent (post-resume content) and a RunCompletedEvent.
    After the resume stream completes, ``aget_run_output`` /
    ``aget_last_run_output`` return the final answer (matching Agno's
    real session-DB-backed behaviour).
    """

    def __init__(self, run_id: str = "fake-run-1", session_id: str = "fake-session") -> None:
        self.run_id = run_id
        self.session_id = session_id
        self._requirement = _FakeRequirement()
        self.arun_calls: list[Any] = []
        self.acontinue_run_calls: list[dict] = []
        self._final_content = "Sub-agent post-resume output"
        self._run_output = _FakeRunOutput()

    def arun(self, task: str, stream: bool = False) -> Any:
        self.arun_calls.append(task)
        return self._initial_stream()

    async def _initial_stream(self):
        yield RunStartedEvent(
            run_id=self.run_id,
            session_id=self.session_id,
            agent_id="fake",
            agent_name="fake",
        )
        yield RunPausedEvent(
            run_id=self.run_id,
            session_id=self.session_id,
            agent_id="fake",
            agent_name="fake",
            requirements=[self._requirement],
        )

    def acontinue_run(self, **kwargs: Any) -> Any:
        # Capture full kwargs so tests can assert that session_id and
        # run_id are both passed (Agno requires both for resume).
        self.acontinue_run_calls.append(kwargs)
        return self._continue_stream()

    async def _continue_stream(self):
        yield RunContentEvent(
            content=self._final_content,
            run_id=self.run_id,
            agent_id="fake",
            agent_name="fake",
        )
        yield RunCompletedEvent(
            content=self._final_content,
            run_id=self.run_id,
            agent_id="fake",
            agent_name="fake",
        )
        # Match Agno: once the run completes, the RunOutput in the
        # session DB has the canonical content.
        self._run_output.content = self._final_content

    async def aget_run_output(self, run_id: str, session_id: str | None = None) -> Any:
        if run_id == self.run_id and (session_id is None or session_id == self.session_id):
            return self._run_output if self._run_output.content else None
        return None

    async def aget_last_run_output(self, session_id: str | None = None) -> Any:
        if session_id is None or session_id == self.session_id:
            return self._run_output if self._run_output.content else None
        return None


class _FakeAgentCompletedOnly(_FakeAgent):
    """Variant: post-resume content arrives ONLY in RunCompletedEvent
    (no RunContentEvent deltas). Models sometimes do this when their
    follow-up generation is short or non-streaming. Verifies the
    spawn still returns the final answer (read from run_response)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._final_content = "Final answer delivered in completion"

    async def _continue_stream(self):
        yield RunCompletedEvent(
            content=self._final_content,
            run_id=self.run_id,
            agent_id="fake",
            agent_name="fake",
        )
        self._run_output.content = self._final_content


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll ``predicate()`` until truthy or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class TestSubAgentHITLPlumbing:
    """The pause-bridge plumbing in isolation — no LLM, no backend."""

    @pytest.mark.asyncio
    async def test_pause_surfaces_in_coordinator(self):
        """Sub-agent pause → coordinator has the requirement → spawn task is blocked."""
        coordinator = SubAgentHITLCoordinator()
        agent = _FakeAgent()

        spawn_task = asyncio.create_task(
            _run_agent_streaming(agent, "test", hitl_coordinator=coordinator)
        )

        # Coordinator should receive the requirement within a tick or two
        arrived = await _wait_until(lambda: bool(coordinator._pending))
        assert arrived, "coordinator never received the sub-agent requirement"

        # spawn_task is blocked waiting for resolution
        assert not spawn_task.done(), "spawn_task completed before resolve"

        # Clean up: resolve so the task can finish
        req_id = next(iter(coordinator._pending))
        coordinator.resolve(req_id, "confirm")
        await asyncio.wait_for(spawn_task, timeout=2)

    @pytest.mark.asyncio
    async def test_confirm_resumes_subagent(self):
        """Confirm → spawn calls acontinue_run with the requirement → final content captured."""
        coordinator = SubAgentHITLCoordinator()
        agent = _FakeAgent()

        spawn_task = asyncio.create_task(
            _run_agent_streaming(agent, "test", hitl_coordinator=coordinator)
        )

        await _wait_until(lambda: bool(coordinator._pending))
        req_id = next(iter(coordinator._pending))

        # Simulate user clicking "allow"
        handled = coordinator.resolve(req_id, "confirm")
        assert handled, "coordinator.resolve should report it owned the req"

        result, activity = await asyncio.wait_for(spawn_task, timeout=2)

        # The fake's requirement was confirmed
        assert agent._requirement.confirmed
        assert not agent._requirement.rejected

        # acontinue_run was called with the right run_id, session_id,
        # and the same requirement instance — that's how Agno actually
        # resumes. The pool now wires ``db=session.db`` into every
        # specialist so the paused run is persisted and Agno can resolve
        # ``(run_id, session_id)`` back to it. Before this, resume failed
        # with "No runs found for run ID …".
        assert len(agent.acontinue_run_calls) == 1
        call = agent.acontinue_run_calls[0]
        assert call["run_id"] == agent.run_id
        assert call["session_id"] == agent.session_id
        assert call["requirements"] == [agent._requirement]

        # The post-resume content lands in the spawn's result string
        assert "post-resume output" in result

    @pytest.mark.asyncio
    async def test_reject_passes_note_through(self):
        """Reject → requirement marked rejected with the standard note."""
        coordinator = SubAgentHITLCoordinator()
        agent = _FakeAgent()

        spawn_task = asyncio.create_task(
            _run_agent_streaming(agent, "test", hitl_coordinator=coordinator)
        )

        await _wait_until(lambda: bool(coordinator._pending))
        req_id = next(iter(coordinator._pending))

        coordinator.resolve(req_id, "reject")
        await asyncio.wait_for(spawn_task, timeout=2)

        assert agent._requirement.rejected
        assert agent._requirement.reject_note == "User denied"

    @pytest.mark.asyncio
    async def test_no_coordinator_passes_silently(self):
        """Without a coordinator wired, the pause is a no-op (logs only) — the
        spawn task completes without resuming. Confirms we haven't broken
        the no-coordinator code path."""
        agent = _FakeAgent()

        spawn_task = asyncio.create_task(_run_agent_streaming(agent, "test", hitl_coordinator=None))

        # No coordinator → the loop just falls through the pause event.
        # Should complete (no resume happens, but no infinite wait either).
        result, activity = await asyncio.wait_for(spawn_task, timeout=2)
        assert agent.acontinue_run_calls == []  # never resumed
        # Activity log should mention the pause was dropped.
        assert any("paused: no HITL bridge" in line for line in activity)

    @pytest.mark.asyncio
    async def test_coordinator_cleanup_after_resolve(self):
        """After spawn finishes, the coordinator's registry is empty —
        no leaked requirements."""
        coordinator = SubAgentHITLCoordinator()
        agent = _FakeAgent()

        spawn_task = asyncio.create_task(
            _run_agent_streaming(agent, "test", hitl_coordinator=coordinator)
        )

        await _wait_until(lambda: bool(coordinator._pending))
        req_id = next(iter(coordinator._pending))
        coordinator.resolve(req_id, "confirm")
        await asyncio.wait_for(spawn_task, timeout=2)

        assert coordinator._pending == {}, "coordinator should be empty after resolve"
        assert not coordinator.has_unresolved()

    @pytest.mark.asyncio
    async def test_list_new_pending_only_returns_unsurfaced(self):
        """The backend's polling logic relies on this idempotency:
        ``list_new_pending`` returns each entry exactly once."""
        coordinator = SubAgentHITLCoordinator()

        req_a = _FakeRequirement()
        req_b = _FakeRequirement()

        id_a = await coordinator.push_requirement(req_a, run_id="run-a")
        id_b = await coordinator.push_requirement(req_b, run_id="run-b")

        first = coordinator.list_new_pending()
        assert sorted(rid for rid, _ in first) == sorted([id_a, id_b])

        # Second call returns nothing — both already surfaced.
        assert coordinator.list_new_pending() == []

    @pytest.mark.asyncio
    async def test_post_resume_content_in_completed_event(self):
        """When the post-HITL response arrives only in RunCompletedEvent
        (no streaming deltas), we still capture it. Reproduces the
        truncation we saw with MiniMax: tool ran, run completed with the
        final answer, but we returned an empty string because we only
        looked at RunContentEvent."""
        coordinator = SubAgentHITLCoordinator()
        agent = _FakeAgentCompletedOnly()

        spawn_task = asyncio.create_task(
            _run_agent_streaming(agent, "test", hitl_coordinator=coordinator)
        )

        await _wait_until(lambda: bool(coordinator._pending))
        req_id = next(iter(coordinator._pending))
        coordinator.resolve(req_id, "confirm")

        result, activity = await asyncio.wait_for(spawn_task, timeout=2)
        assert "Final answer delivered in completion" in result, (
            f"expected post-resume completion content in result, got: {result!r}"
        )

    @pytest.mark.asyncio
    async def test_agent_path_rides_along(self):
        """The agent dispatch chain is preserved in the coordinator entry
        so the FE dialog can show ``architect → reviewer`` rather than
        just ``Bash``."""
        coordinator = SubAgentHITLCoordinator()
        req = _FakeRequirement()

        path = ["architect", "reviewer"]
        req_id = await coordinator.push_requirement(req, run_id="run-xyz", agent_path=path)

        entries = coordinator.list_new_pending()
        assert len(entries) == 1
        rid, entry = entries[0]
        assert rid == req_id
        assert entry.agent_path == path
        # Mutating the original list must not bleed into the entry.
        path.append("editor")
        assert entry.agent_path == ["architect", "reviewer"]


class TestPendingEntryModel:
    """Post-refactor ``_PendingEntry`` is a Pydantic BaseModel (Rule 1
    compliance — was a dataclass before). These tests lock in that the
    coordinator's public methods still round-trip data cleanly and
    that the mutable fields (``surfaced`` + ``event``) still flip
    from within the coordinator's methods."""

    def test_pending_entry_is_pydantic(self):
        assert issubclass(_PendingEntry, BaseModel)

    def test_pending_entry_defaults_and_mutability(self):
        entry = _PendingEntry(requirement=object(), run_id="r1")
        # Defaults survive the dataclass → BaseModel migration.
        assert entry.agent_path == []
        assert entry.surfaced is False
        assert isinstance(entry.event, asyncio.Event)
        # Coordinator mutates ``surfaced`` and ``event`` from its
        # methods — must remain assignable post-refactor.
        entry.surfaced = True
        entry.event.set()
        assert entry.surfaced is True
        assert entry.event.is_set() is True

    @pytest.mark.asyncio
    async def test_coordinator_still_works_end_to_end(self):
        """Regression net: the pydantic migration must not break the
        push → list_new_pending → resolve lifecycle."""
        coord = SubAgentHITLCoordinator()
        req = _FakeRequirement()
        req_id = await coord.push_requirement(req, run_id="run-x", agent_path=["arch"])
        # Newly-pushed entry surfaces exactly once.
        new_1 = coord.list_new_pending()
        assert len(new_1) == 1
        assert new_1[0][0] == req_id
        assert new_1[0][1].run_id == "run-x"
        assert new_1[0][1].agent_path == ["arch"]
        # Second call returns nothing (already surfaced).
        assert coord.list_new_pending() == []
        assert coord.has_unresolved() is True
        # Resolve → future wait resolves.
        assert coord.resolve(req_id, "confirm") is True
        resolved = await coord.wait_resolved(req_id)
        assert resolved is req
        # Confirm side effect on the fake requirement.
        assert req.confirmed is True
        assert coord.has_unresolved() is False
        # Cleanup drops the record.
        coord.cleanup(req_id)
        assert coord.list_new_pending() == []
