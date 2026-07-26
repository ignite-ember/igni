"""End-to-end smoke for the workflow runner.

Spawns the Node bridge against a tiny on-disk workflow file and
verifies the runner drains the structured event stream, persists
+ pushes every event, and tears down cleanly on a one-phase
run. Skipped unless Node is on PATH.

This is the smallest slice that exercises the full subprocess
lifecycle (spawn → drain → push → exit → cleanup). The
heavier ``refactor-to-standards`` workflow has a 5-phase
end-to-end test gated behind ``@pytest.mark.integration`` in
``test_workflow_runner.py``.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.workflow_runner import WorkflowRunner


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node not on PATH; workflow runner needs the Node bridge",
)


async def test_runner_drains_simple_workflow(tmp_path: Path) -> None:
    """One phase, no agents, return value. Asserts all 3 events
    (workflow_started, phase_started, phase_completed,
    workflow_completed) arrive + persist + push."""
    workflows_dir = tmp_path / ".claude" / "workflows"
    workflows_dir.mkdir(parents=True)
    wf_path = workflows_dir / "smoke.mjs"
    wf_path.write_text(
        """
export const meta = { name: "smoke", description: "test", phases: [{title: "only", detail: ""}] }
phase("only")
log("hello")
return { ok: true }
""".strip()
        + "\n"
    )

    push = MagicMock()
    push._schedule_push = MagicMock()
    session = MagicMock()
    session.id = "sess_smoke"
    session.append_event = AsyncMock()
    # main_team is unused here (no agent() calls in the workflow).
    session.main_team = MagicMock()

    runner = WorkflowRunner(project_dir=tmp_path, push=push)
    run_id = await runner.run(name="smoke", args={}, session=session)
    assert run_id.startswith("wf_")
    assert run_id in runner._runs

    # Poll until the run is unregistered (the await_completion
    # task does that on natural exit). Each await iteration
    # yields to the loop so the runner's tasks make progress.
    for _ in range(200):
        if run_id not in runner._runs:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"workflow did not complete within 10s; run_id still tracked")

    # 4 events: workflow_started, phase_started, log, phase_completed,
    # workflow_completed. We assert >=4 because the log event is
    # a "capture for diagnostics" path that the reducer ignores.
    assert session.append_event.await_count >= 4
    types = [c.args[1]["type"] for c in session.append_event.await_args_list]
    assert "workflow_started" in types
    assert "phase_started" in types
    assert "phase_completed" in types
    assert "workflow_completed" in types

    # The push side fired the same number of times.
    assert push._schedule_push.call_count == session.append_event.await_count
    # Run is no longer tracked (await_completion unregisters).
    assert run_id not in runner._runs
