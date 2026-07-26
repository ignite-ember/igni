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


# ── Runner edge cases (cancel, cwd-relative, agent timeout) ──

async def test_runner_spawns_user_layer_workflow_with_cwd_relative_path(
    tmp_path: Path,
) -> None:
    """A workflow in ``.ember/workflows/`` is spawned with the
    same ``cwd=project_dir`` as a team-layer workflow, and the
    ``path`` field on the discovery envelope is a project-relative
    path that the runner resolves to an absolute path before
    passing it to the Node subprocess.

    This is the regression test for the user-layer feature:
    the user-layer path passes through the same spawn pipeline
    as the team-layer path.
    """
    user_dir = tmp_path / ".ember" / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "personal.mjs").write_text(
        "export const meta = { name: 'personal' }\n"
    )

    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        if "--discovery" in args:
            path = Path(args[2])
            line = json.dumps(
                {
                    "ts": 0,
                    "run_id": "discovery",
                    "seq": 0,
                    "type": "workflow_meta",
                    "payload": {
                        "meta": {"name": path.stem, "phases": []},
                        "path": str(path.relative_to(tmp_path)),
                    },
                }
            )
            proc = _FakeProcess([], exit_code=0)
            proc.communicate = AsyncMock(
                return_value=((line + "\n").encode("utf-8"), b"")
            )
            proc.returncode = 0
            return proc
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProcess([], exit_code=0)

    import ember_code.backend.workflow_runner as runner_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)
        runner = WorkflowRunner(project_dir=tmp_path, push=MagicMock())
        session = MagicMock()
        session.id = "sess_x"
        run_id = await runner.run(name="personal", args={}, session=session)
        assert run_id.startswith("wf_")
        # The Node subprocess was spawned with the project dir as cwd.
        assert captured["cwd"] == str(tmp_path)
        # The path passed to the subprocess is the absolute path to
        # the workflow file (under the project root), not just the
        # ``.ember/workflows/personal.mjs`` relative string.
        path_arg = [a for a in captured["args"] if "personal" in str(a)][0]
        assert Path(path_arg).is_absolute()
        assert str(path_arg).endswith(".ember/workflows/personal.mjs")


async def test_runner_cancel_sends_cancel_message_and_sends_sigterm(tmp_path: Path) -> None:
    """``cancel(workflow_run_id)`` writes a ``{"type":"cancel"}`` line
    to the subprocess stdin, then waits for natural exit. If the
    subprocess doesn't exit within ``CANCEL_GRACE_SECONDS``, the
    runner sends SIGTERM, then SIGKILL as a last resort.

    We exercise the cancel-message path here; the SIGTERM/SIGKILL
    escalation is covered separately by the subprocess integration
    tests (which need a real Node — outside the unit-test surface).
    """
    from ember_code.backend.workflow_runner import (
        CANCEL_GRACE_SECONDS,
        WorkflowRunner,
    )

    proc = _FakeProcess([], exit_code=0)
    proc.returncode = None  # not yet exited
    state = _RunState(proc=proc, workflow_run_id="wf_cancel", name="smoke")

    written: list[bytes] = []

    class _CancelStream:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    proc.stdin = _CancelStream()

    push = MagicMock()
    push._schedule_push = MagicMock()
    runner = WorkflowRunner(project_dir=tmp_path, push=push)
    runner._runs["wf_cancel"] = state

    async def fake_wait(*a, **kw):
        # Subprocess exits cleanly after the cancel message.
        proc.returncode = 0
        return 0

    proc.wait = fake_wait  # type: ignore[method-assign]

    # Patch asyncio.wait_for to call fake_wait directly (avoids
    # the actual asyncio.wait_for scheduling).
    import ember_code.backend.workflow_runner as runner_mod
    real_wait_for = runner_mod.asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        # Make sure the cancel message is written before we "wait"
        # for the subprocess to exit.
        if timeout == CANCEL_GRACE_SECONDS:
            return await coro
        return await real_wait_for(coro, timeout=0.1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "wait_for", fake_wait_for)
        await runner.cancel("wf_cancel")

    # The cancel message was written to stdin.
    assert len(written) == 1
    assert json.loads(written[0].decode()) == {"type": "cancel"}


async def test_agent_bridge_timeout_fires_agent_response_error(
    tmp_path: Path,
) -> None:
    """If the agent call exceeds ``timeoutSeconds``, the bridge
    sends an ``agent_response{ok: false, error: timeout message}``
    and rejects the agent's promise — the workflow continues
    on the next event.
    """
    import ember_code.backend.workflow_runner as runner_mod
    from ember_code.backend.workflow_runner import (
        _RunState,
    )

    proc = _FakeProcess([], exit_code=0)
    state = _RunState(proc=proc, workflow_run_id="wf_t", name="smoke")

    push = MagicMock()
    push._schedule_push = MagicMock()
    runner = WorkflowRunner(project_dir=tmp_path, push=push)

    session = MagicMock()
    # team.arun hangs forever; the bridge must time it out.
    async def hang(*a, **kw):
        await asyncio.sleep(60)
        return "should not return"
    session.main_team.arun = hang

    ev = MagicMock()
    ev.payload = {
        "id": "req_slow",
        "prompt": "slow",
        "label": "slow",
        "phase": "S",
        "timeout_seconds": 1,
    }

    written: list[bytes] = []

    class _Stream:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    proc.stdin = _Stream()

    real_wait_for = runner_mod.asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        # Don't actually wait the full timeout — force the timeout
        # path. We do this by setting the timeout to a tiny value
        # and letting ``wait_for`` raise TimeoutError naturally.
        if timeout > 1:
            return await real_wait_for(coro, timeout=0.05)
        return await real_wait_for(coro, timeout=timeout)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "wait_for", fast_wait_for)
        await runner._handle_agent_request(state, session, ev)  # type: ignore[attr-defined]

    # The bridge wrote an agent_response with ok=False and an error.
    assert len(written) == 1
    payload = json.loads(written[0].decode())
    assert payload["type"] == "agent_response"
    assert payload["id"] == "req_slow"
    assert payload["ok"] is False
    assert "timed out" in payload["error"]


async def test_runner_passes_args_to_subprocess_as_json_string(
    tmp_path: Path,
) -> None:
    """The runner serializes the ``args`` dict to JSON and passes
    it as the last CLI arg (``--args``). The Node runtime parses
    it back. This is the wire contract for the user-layer
    workflow's args object.
    """
    user_dir = tmp_path / ".ember" / "workflows"
    user_dir.mkdir(parents=True)
    (user_dir / "personal.mjs").write_text(
        "export const meta = { name: 'personal' }\n"
    )

    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        # Discovery subprocesses pass ``--discovery``; the run
        # subprocess passes ``--args <json>``. Handle both.
        if "--discovery" in args:
            path = Path(args[2])
            line = json.dumps(
                {
                    "ts": 0,
                    "run_id": "discovery",
                    "seq": 0,
                    "type": "workflow_meta",
                    "payload": {
                        "meta": {"name": path.stem, "phases": []},
                        "path": str(path.relative_to(tmp_path)),
                    },
                }
            )
            proc = _FakeProcess([], exit_code=0)
            proc.communicate = AsyncMock(
                return_value=((line + "\n").encode("utf-8"), b"")
            )
            proc.returncode = 0
            return proc
        # Run subprocess: capture the --args value.
        i = list(args).index("--args")
        captured_args.append(args[i + 1])
        return _FakeProcess([], exit_code=0)

    import ember_code.backend.workflow_runner as runner_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner_mod.asyncio, "create_subprocess_exec", fake_exec)
        runner = WorkflowRunner(project_dir=tmp_path, push=MagicMock())
        session = MagicMock()
        session.id = "sess_x"
        await runner.run(
            name="personal",
            args={"file": "src/example.py", "tag": "demo"},
            session=session,
        )

    assert len(captured_args) == 1
    parsed = json.loads(captured_args[0])
    assert parsed == {"file": "src/example.py", "tag": "demo"}

# Quick patch: redefine fake_exec to handle discovery too.
# (Replaces the previous fake_exec in the args test.)
