"""Tests for the workflow RPC handlers.

Covers :class:`WorkflowsRpcHandler` (the BE-side handlers that
sit behind the ``list_workflows`` and ``run_workflow`` RPCs
the FE calls from the ``/workflows`` slash command and the
``?demo=workflow`` demo). The handler is small — it just
delegates to :class:`WorkflowRunner` — but it has two error
paths the unit tests don't otherwise cover:

  - ``list_workflows`` returns an empty list when no
    ``WorkflowRunner`` is attached to the backend
    (e.g. during the very first boot frame).
  - ``run_workflow`` surfaces the runner's FileNotFoundError
    / RuntimeError to the FE as a regular RPC error.

These tests mock the runner + a fake session so the handler
runs as if it were wired into a real BackendServer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.rpc_handlers.workflows import WorkflowsRpcHandler
from ember_code.backend.schemas_workflows import WorkflowMetaEnvelope
from ember_code.protocol.rpc import RpcMethod


def _envelope(name: str = "smoke", path: str = ".claude/workflows/smoke.mjs") -> WorkflowMetaEnvelope:
    """A canned discovery result the runner would emit."""
    from ember_code.backend.schemas_workflows import WorkflowMeta

    return WorkflowMetaEnvelope(
        meta=WorkflowMeta(
            name=name,
            description=f"smoke {name}",
            whenToUse="tests",
            phases=[],
        ),
        path=path,
    )


def _make_handler(*, runner: MagicMock | None, session: object | None = None) -> WorkflowsRpcHandler:
    """Build a handler with a fake ctx — bypasses the real
    ``BackendServer`` so we can test the handler in isolation.
    """
    backend = MagicMock()
    backend.workflow_runner = runner
    ctx = MagicMock()
    ctx.backend = backend
    handler = WorkflowsRpcHandler(ctx)
    # Override the session lookup the handler does at runtime.
    handler._resolve_session = MagicMock(return_value=session or MagicMock())  # type: ignore[attr-defined]
    return handler


async def test_list_workflows_returns_discovery_output() -> None:
    """``list_workflows`` returns the runner's discovery output
    as a list of dicts (the wire shape the FE expects)."""
    runner = MagicMock()
    runner.list_workflows = AsyncMock(
        return_value=[
            _envelope("alpha", ".claude/workflows/alpha.mjs"),
            _envelope("beta", ".ember/workflows/beta.mjs"),
        ]
    )
    handler = _make_handler(runner=runner)

    out = await handler.list_workflows({})

    assert out == [
        {
            "name": "alpha",
            "description": "smoke alpha",
            "whenToUse": "tests",
            "phases": [],
            "path": ".claude/workflows/alpha.mjs",
        },
        {
            "name": "beta",
            "description": "smoke beta",
            "whenToUse": "tests",
            "phases": [],
            "path": ".ember/workflows/beta.mjs",
        },
    ]


async def test_list_workflows_returns_empty_when_no_runner() -> None:
    """During boot, before the runner is attached, the handler
    should return ``[]`` (not raise). The FE renders an empty
    suggestion list until the BE is ready."""
    handler = _make_handler(runner=None)
    assert await handler.list_workflows({}) == []


async def test_run_workflow_returns_workflow_run_id_without_blocking() -> None:
    """``run_workflow`` returns synchronously with the
    ``workflow_run_id`` the runner emitted; the actual subprocess
    runs in the background and streams events later."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value="wf_abc123def456")
    handler = _make_handler(runner=runner)

    out = await handler.run_workflow(
        {
            "name": "refactor-to-standards",
            "args": {"file": "src/example.py"},
            "session_id": "sess_xyz",
        }
    )

    assert out == {
        "workflow_run_id": "wf_abc123def456",
        "name": "refactor-to-standards",
    }
    runner.run.assert_awaited_once()
    # The runner received the parsed args + the resolved session.
    call = runner.run.await_args
    assert call.kwargs == {
        "name": "refactor-to-standards",
        "args": {"file": "src/example.py"},
        "session": handler._resolve_session.return_value,
    }


async def test_run_workflow_defaults_args_to_empty_dict() -> None:
    """A missing ``args`` field is treated as ``{}`` (matches the
    Pydantic model on the BE side)."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value="wf_q")
    handler = _make_handler(runner=runner)

    await handler.run_workflow({"name": "smoke", "session_id": "sess_x"})

    assert runner.run.await_args.kwargs["args"] == {}


async def test_run_workflow_surfaces_file_not_found() -> None:
    """If the runner can't find the named workflow, the
    FileNotFoundError propagates so the BE's RPC layer returns
    it as a JSON-RPC error to the FE."""
    runner = MagicMock()
    runner.run = AsyncMock(
        side_effect=FileNotFoundError("workflow 'missing' not found")
    )
    handler = _make_handler(runner=runner)

    with pytest.raises(FileNotFoundError, match="missing"):
        await handler.run_workflow({"name": "missing", "session_id": "sess_x"})


async def test_run_workflow_surfaces_runtime_error() -> None:
    """If the workflow_runtime.mjs is missing on disk, the
    runner raises a RuntimeError; the handler propagates it."""
    runner = MagicMock()
    runner.run = AsyncMock(
        side_effect=RuntimeError("workflow runtime missing at /tmp/x.mjs")
    )
    handler = _make_handler(runner=runner)

    with pytest.raises(RuntimeError, match="workflow runtime missing"):
        await handler.run_workflow({"name": "smoke", "session_id": "sess_x"})


async def test_run_workflow_rejects_empty_name() -> None:
    """Empty name is a programmer error, not a runtime one.
    The handler raises ValueError synchronously without
    touching the runner."""
    runner = MagicMock()
    handler = _make_handler(runner=runner)
    with pytest.raises(ValueError, match="'name' is required"):
        await handler.run_workflow({"session_id": "sess_x"})
    runner.run.assert_not_called()


async def test_run_workflow_rejects_non_dict_args() -> None:
    """``args`` must be a dict. A scalar or list is a programmer
    error — the FE never sends anything else."""
    runner = MagicMock()
    handler = _make_handler(runner=runner)
    with pytest.raises(TypeError, match="'args' must be a dict"):
        await handler.run_workflow({"name": "smoke", "args": "oops", "session_id": "sess_x"})
    runner.run.assert_not_called()


async def test_resolve_session_falls_back_to_default_when_id_empty() -> None:
    """The handler's ``_resolve_session`` falls back to the
    default backend session when the caller passes an empty
    ``session_id`` (matches the orchestrator's
    ``session-id: ''`` pattern in the slash-command path)."""
    backend = MagicMock()
    backend.sessions.find = MagicMock(return_value=None)
    ctx = MagicMock()
    ctx.backend = backend
    handler = WorkflowsRpcHandler(ctx)
    # Empty id → default session.
    out = handler._resolve_session("")
    assert out is backend._session
    # Non-empty id, no runtime match → still default.
    out = handler._resolve_session("sess_unknown")
    assert out is backend._session
    # Non-empty id, runtime match → that runtime's session.
    rt = MagicMock()
    rt.backend = MagicMock()
    rt.backend._session = MagicMock(name="rt_session")
    backend.sessions.find = MagicMock(return_value=rt)
    out = handler._resolve_session("sess_known")
    assert out is rt.backend._session


async def test_rpc_method_enum_values_match_wire_string() -> None:
    """The wire strings the FE uses to call these RPCs match the
    RpcMethod enum values. If either changes, this test
    fails and the change is caught at the seam (no silent
    string drift)."""
    assert RpcMethod.LIST_WORKFLOWS.value == "list_workflows"
    assert RpcMethod.RUN_WORKFLOW.value == "run_workflow"

async def test_cancel_workflow_returns_cancelled_true_when_run_exists() -> None:
    """``cancel_workflow`` calls the runner's ``cancel`` and
    surfaces the boolean as a dict."""
    runner = MagicMock()
    runner.cancel = AsyncMock(return_value=True)
    handler = _make_handler(runner=runner)

    out = await handler.cancel_workflow({"workflow_run_id": "wf_abc"})

    assert out == {"cancelled": True, "workflow_run_id": "wf_abc"}
    runner.cancel.assert_awaited_once_with("wf_abc")


async def test_cancel_workflow_returns_cancelled_false_when_no_run() -> None:
    """If the runner doesn't have the run (already completed,
    cancelled, or unknown id), the handler returns ``cancelled:
    false`` — the caller's UI shows "already stopped"."""
    runner = MagicMock()
    runner.cancel = AsyncMock(return_value=False)
    handler = _make_handler(runner=runner)

    out = await handler.cancel_workflow({"workflow_run_id": "wf_unknown"})

    assert out == {"cancelled": False, "workflow_run_id": "wf_unknown"}


async def test_cancel_workflow_surfaces_runtime_error() -> None:
    """If the runner's cancel hits a runtime error (e.g. the
    BE hasn't booted the runner), the handler propagates it."""
    runner = MagicMock()
    runner.cancel = AsyncMock(
        side_effect=RuntimeError("workflow_runner not initialised")
    )
    handler = _make_handler(runner=runner)

    with pytest.raises(RuntimeError, match="workflow_runner not initialised"):
        await handler.cancel_workflow({"workflow_run_id": "wf_x"})


async def test_cancel_workflow_rejects_empty_id() -> None:
    """Empty id is a programmer error — fail fast, don't touch
    the runner."""
    runner = MagicMock()
    handler = _make_handler(runner=runner)
    with pytest.raises(ValueError, match="'workflow_run_id' is required"):
        await handler.cancel_workflow({})
    runner.cancel.assert_not_called()


def test_rpc_method_includes_cancel_workflow() -> None:
    """The wire string for the new RPC matches the enum value."""
    assert RpcMethod.CANCEL_WORKFLOW.value == "cancel_workflow"
