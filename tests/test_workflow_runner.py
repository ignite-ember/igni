"""Unit tests for the BE workflow runner.

Covers the three pieces of the runner's surface that don't need a
live neo4j / live LLM:

1. :class:`WorkflowDiscovery` — metadata read from
   ``.claude/workflows/*.mjs`` via the runtime's ``--discovery``
   short-circuit. Mocked subprocess emits canned ``workflow_meta``
   lines; the discovery aggregates + caches by mtime.

2. :class:`WorkflowRunner._drain_stdout` — line-by-line event
   parsing, with the persisted + push side effects exercised
   against an in-memory append + a fake push bridge.

3. The agent bridge — an ``agent_request`` event fans out a
   coroutine that calls ``session.main_team.arun`` and writes the
   matching ``agent_response`` back to the subprocess stdin.

The e2e path (real subprocess, real session, real push) lives in
``tests/test_workflow_e2e.py`` and is gated behind
``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.schemas_workflows import WorkflowMeta
from ember_code.backend.workflow_runner import WorkflowDiscovery, WorkflowRunner, _RunState


def _meta_line(name: str = "smoke", phases: int = 2) -> str:
    """One well-formed ``workflow_meta`` line as the runtime emits it."""
    return json.dumps(
        {
            "ts": 0,
            "run_id": "discovery",
            "seq": 0,
            "type": "workflow_meta",
            "payload": {
                "meta": {
                    "name": name,
                    "description": f"smoke test {name}",
                    "whenToUse": "tests",
                    "phases": [{"title": f"P{i}", "detail": ""} for i in range(phases)],
                },
                "path": f".claude/workflows/{name}.mjs",
            },
        }
    )


def _event(type_: str, payload: dict, seq: int = 0, run_id: str = "wf_abc") -> str:
    return json.dumps(
        {"ts": 1_700_000_000_000 + seq, "run_id": run_id, "seq": seq, "type": type_, "payload": payload}
    )


class _FakeStream:
    """Minimal subprocess stdout/stdin double for drain tests."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.written: list[bytes] = []

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _FakeProcess:
    def __init__(self, lines: list[bytes], exit_code: int = 0) -> None:
        self.stdout = _FakeStream(lines)
        self.stdin = self.stdout  # share for test simplicity
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._waiters: list[asyncio.Future[int]] = []

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code


async def test_discovery_aggregates_and_caches(tmp_path: Path) -> None:
    """Discovery reads each workflow's meta; second call is cached."""
    workflow_dir = tmp_path / ".claude" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "alpha.mjs").write_text("// alpha body\n")
    (workflow_dir / "beta.mjs").write_text("// beta body\n")

    # Patch the runtime call to return canned meta lines.
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        # args: (node, runtime, path, --discovery, --workflow-run-id, discovery)
        calls.append(args)
        path = Path(args[2])
        name = path.stem
        line = _meta_line(name=name, phases=3)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(
            (line + "\n").encode("utf-8"),
            b"",
        ))
        return proc

    # Patch the symbol where the runner uses it (its module's
    # local binding), NOT the asyncio module — direct attribute
    # assignment on asyncio leaks across other tests.
    import ember_code.backend.workflow_runner as runner_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery(project_dir=tmp_path)
        first = await disc.list_workflows()
        assert [env.meta.name for env in first] == ["alpha", "beta"], (
            f"got {[e.meta.name for e in first]}"
        )
        # Discovery calls the runtime once per workflow file.
        assert len(calls) == 2
        # Second call reuses cache (no extra subprocess calls).
        second = await disc.list_workflows()
        assert second == first
        assert len(calls) == 2  # no new calls


async def test_discovery_skips_when_no_dir(tmp_path: Path) -> None:
    """Empty project: no workflows dir → empty list, no subprocess."""
    disc = WorkflowDiscovery(project_dir=tmp_path)
    assert await disc.list_workflows() == []


async def test_resolve_unknown_returns_none(tmp_path: Path) -> None:
    disc = WorkflowDiscovery(project_dir=tmp_path)
    assert await disc.resolve("does-not-exist") is None


async def test_user_layer_shadows_team_layer(tmp_path: Path) -> None:
    """A workflow with the same name in both layers is resolved to
    the user-layer copy. ``list_workflows`` returns one entry
    per name (no duplicates). The path field reports the
    user-layer location.
    """
    team_dir = tmp_path / ".claude" / "workflows"
    user_dir = tmp_path / ".ember" / "workflows"
    team_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    (team_dir / "shared.mjs").write_text(
        "export const meta = { name: 'team-version' }\n"
    )
    (user_dir / "shared.mjs").write_text(
        "export const meta = { name: 'user-version' }\n"
    )
    (user_dir / "personal.mjs").write_text(
        "export const meta = { name: 'personal' }\n"
    )

    async def fake_exec(*args, **kwargs):
        path = Path(args[2])
        name = path.stem
        # Read the file's meta line to determine which copy ran.
        with open(path) as f:
            first_line = f.readline().strip()
        if "team-version" in first_line:
            label = "team-version"
        else:
            label = "user-version" if "user-version" in first_line else "personal"
        line = json.dumps(
            {
                "ts": 0,
                "run_id": "discovery",
                "seq": 0,
                "type": "workflow_meta",
                "payload": {
                    "meta": {"name": label, "phases": []},
                    "path": str(path.relative_to(tmp_path)),
                },
            }
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=((line + "\n").encode("utf-8"), b"")
        )
        proc.returncode = 0
        return proc

    import ember_code.backend.workflow_runner as runner_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery(project_dir=tmp_path)
        result = await disc.list_workflows()
        # Two unique workflows (one deduplicated by shadow).
        assert [env.meta.name for env in result] == ["personal", "user-version"]
        # The shadowed "shared" name resolves to user-version.
        shared = await disc.resolve("shared")
        assert shared is not None
        assert shared.meta.name == "user-version"
        assert shared.path == ".ember/workflows/shared.mjs"


async def test_discovery_scans_both_layers_independently(tmp_path: Path) -> None:
    """The two layers are scanned independently — a project with
    only a user layer (no team layer) still works, and vice versa.
    """
    user_dir = tmp_path / ".ember" / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "user-only.mjs").write_text(
        "export const meta = { name: 'user-only' }\n"
    )

    async def fake_exec(*args, **kwargs):
        path = Path(args[2])
        line = json.dumps(
            {
                "ts": 0,
                "run_id": "discovery",
                "seq": 0,
                "type": "workflow_meta",
                "payload": {
                    "meta": {"name": "user-only", "phases": []},
                    "path": str(path.relative_to(tmp_path)),
                },
            }
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=((line + "\n").encode("utf-8"), b"")
        )
        proc.returncode = 0
        return proc

    import ember_code.backend.workflow_runner as runner_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)
        disc = WorkflowDiscovery(project_dir=tmp_path)
        result = await disc.list_workflows()
        assert [env.meta.name for env in result] == ["user-only"]
        assert result[0].path == ".ember/workflows/user-only.mjs"


async def test_event_drain_persists_and_pushes(tmp_path: Path) -> None:
    """Three stdout lines → three session.append_event + three push calls."""
    lines = [
        _event("workflow_started", {"name": "smoke"}, seq=0).encode("utf-8") + b"\n",
        _event("phase_started", {"phase_id": "p1", "title": "A"}, seq=1).encode("utf-8") + b"\n",
        _event("workflow_completed", {"status": "completed", "result": {"ok": True}}, seq=2).encode("utf-8") + b"\n",
    ]
    proc = _FakeProcess(lines, exit_code=0)

    push = MagicMock()
    push._schedule_push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()

    runner = WorkflowRunner(project_dir=tmp_path, push=push)
    state = _RunState(proc=proc, workflow_run_id="wf_abc", name="smoke")

    await runner._drain_stdout(state, session)

    assert session.append_event.await_count == 3
    assert push._schedule_push.call_count == 3
    # Persisted envelope has the workflow_run_id + event type.
    first_payload = session.append_event.await_args_list[0].args[1]
    assert first_payload["workflow_run_id"] == "wf_abc"
    assert first_payload["type"] == "workflow_started"
    # Final status is captured for the await_completion short-circuit.
    assert state.final_status == "completed"


async def test_synthetic_workflow_failed_on_non_zero_exit(tmp_path: Path) -> None:
    """Subprocess crash mid-run → synthetic workflow_failed event."""
    # Only the workflow_started line — the subprocess then dies.
    lines = [_event("workflow_started", {"name": "x"}, seq=0).encode("utf-8") + b"\n"]
    proc = _FakeProcess(lines, exit_code=2)

    push = MagicMock()
    push._schedule_push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()

    runner = WorkflowRunner(project_dir=tmp_path, push=push)
    state = _RunState(proc=proc, workflow_run_id="wf_boom", name="x")

    # Drain the started event first.
    await runner._drain_stdout(state, session)
    # Then the await_completion notices the non-zero exit and
    # synthesises the failure.
    await runner._await_completion(state, session)

    # The synthetic event is the 2nd call (after the started line).
    types = [c.args[0] for c in session.append_event.await_args_list]
    payloads = [c.args[1] for c in session.append_event.await_args_list]
    assert types == ["workflow_event", "workflow_event"]
    assert payloads[1]["type"] == "workflow_failed"
    assert payloads[1]["payload"]["exit_code"] == 2
    # Pushed too.
    pushed = [c.args[0] for c in push._schedule_push.call_args_list]
    assert pushed == ["workflow_event", "workflow_event"]


async def test_agent_request_bridges_to_team(tmp_path: Path) -> None:
    """An agent_request fans out a coroutine that calls team.arun and
    writes an agent_response back to subprocess stdin."""
    proc = _FakeProcess([], exit_code=0)

    push = MagicMock()
    push._schedule_push = MagicMock()
    session = MagicMock()
    session.append_event = AsyncMock()
    team = MagicMock()
    team.arun = AsyncMock(return_value="agent result text")
    session.main_team = team

    runner = WorkflowRunner(project_dir=tmp_path, push=push)
    state = _RunState(proc=proc, workflow_run_id="wf_call", name="smoke")

    req = json.dumps(
        {
            "ts": 1,
            "run_id": "wf_call",
            "seq": 0,
            "type": "agent_request",
            "payload": {
                "id": "req_42",
                "prompt": "do the thing",
                "label": "test-agent",
                "phase": None,
                "timeout_seconds": 30,
            },
        }
    ).encode("utf-8") + b"\n"
    # Force a read so the drain sees the line.
    proc.stdout._lines = [req]  # type: ignore[attr-defined]

    # Run a single drain iteration by reading the first line manually.
    raw = await proc.stdout.readline()
    event = json.loads(raw.decode("utf-8"))
    assert event["type"] == "agent_request"

    # Invoke the bridge directly.
    from ember_code.backend.schemas_workflows import WorkflowEvent

    ev = WorkflowEvent.model_validate_json(raw.decode("utf-8"))
    await runner._handle_agent_request(state, session, ev)  # type: ignore[attr-defined]

    # team.arun was called with the prompt.
    team.arun.assert_awaited_once()
    call_args = team.arun.await_args
    assert call_args.args[0] == "do the thing"
    # agent_response was written to stdin.
    written = b"".join(proc.stdin.written).decode("utf-8")  # type: ignore[attr-defined]
    assert '"type": "agent_response"' in written
    assert '"id": "req_42"' in written
    assert '"ok": true' in written
    assert '"result": "agent result text"' in written
