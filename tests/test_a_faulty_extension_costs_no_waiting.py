"""An extension that keeps not answering is faulty, and costs nobody any more waiting.

:mod:`ember_code.extensions.faults`: three timeouts or crashes in a row —
hooks and tool calls alike — mark an extension faulty. It is restarted,
the person is told, and while it is faulty its hooks are not waited on:
a fail-closed event is refused at once, a fail-open one goes ahead
without it. The first answer after that clears the mark.
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest
from agno.tools.function import ToolResult

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.extension_gate import DECIDING, ExtensionHookGate
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.extensions.faults import ExtensionFaults
from ember_code.extensions.health import ExtensionHealth
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.processes import ExtensionProcesses
from ember_code.extensions.host.wire import HookAnswer
from ember_code.extensions.registry import ExtensionRefused, ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import ExtensionServiceError
from ember_code.notifications.extension_notices import FaultNotices
from ember_code.notifications.schemas import ExtensionNotice
from tests.host_model_helpers import manifest

CALL = {"session_id": "s-1", "tool_name": "run_shell_command", "tool_args": {"command": "ls"}}
PRE = DECIDING[HookEvent.PRE_TOOL_USE]
STOP = DECIDING[HookEvent.STOP]


class Process:
    """An extension's process on the host socket: hung, or answering ``allow``."""

    def __init__(self, hooks: ExtensionHooks, extension_id: str, *, hung: bool) -> None:
        self.hooks = hooks
        self.extension_id = extension_id
        self.hung = hung
        self.asked = 0
        hooks.connect(extension_id, self.send)

    def send(self, frame: str) -> None:
        message = json.loads(frame)
        if message["id"] is None:
            return
        self.asked += 1
        if self.hung:
            return
        asyncio.get_running_loop().call_soon(
            self.hooks.answered,
            self.extension_id,
            HookAnswer(hook=message["id"], outcome="allow"),
        )


@pytest.fixture
def registry() -> ExtensionRegistry:
    found = ExtensionRegistry()
    found.install(manifest("acme.guard", hooks=["PreToolUse", "Stop"]), state=ExtensionState.READY)
    return found


@pytest.fixture
def faults(registry: ExtensionRegistry) -> ExtensionFaults:
    found = ExtensionFaults(registry)
    return found


@pytest.fixture
def hooks(registry: ExtensionRegistry, faults: ExtensionFaults) -> ExtensionHooks:
    found = ExtensionHooks(timeout=0.2, faults=faults)
    found.attach(registry)
    return found


async def _hang_until_faulty(hooks: ExtensionHooks) -> None:
    for _ in range(ExtensionFaults.LIMIT):
        await hooks.ask(PRE, CALL)


async def test_three_silences_in_a_row_make_it_faulty_and_say_so(
    registry: ExtensionRegistry, faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    told: list[tuple[str, str]] = []
    faults.on_faulty(lambda extension_id, reason: told.append((extension_id, reason)))
    Process(hooks, "acme.guard", hung=True)

    await hooks.ask(PRE, CALL)
    await hooks.ask(PRE, CALL)
    assert not faults.faulty("acme.guard") and told == []
    await hooks.ask(PRE, CALL)

    assert faults.faulty("acme.guard")
    entry = registry.get("acme.guard")
    assert entry is not None and "isn't responding" in entry.faulty
    [(extension_id, reason)] = told
    assert extension_id == "acme.guard" and "restarting" in reason


async def test_a_hung_extension_costs_no_waiting_once_faulty(hooks: ExtensionHooks) -> None:
    process = Process(hooks, "acme.guard", hung=True)
    await _hang_until_faulty(hooks)
    asked = process.asked

    started = time.monotonic()
    refused = await hooks.ask(PRE, CALL)
    skipped = await hooks.ask(STOP, {"session_id": "s-1", "response": ""})
    took = time.monotonic() - started

    assert took < 0.05
    assert process.asked == asked
    # Fail-closed still refuses — at once, saying who.
    assert not refused.allowed and refused.reason == "acme.guard isn't responding."
    # Fail-open goes ahead without it.
    assert skipped.allowed


async def test_an_answer_after_the_restart_clears_it(
    faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    Process(hooks, "acme.guard", hung=True)
    await _hang_until_faulty(hooks)
    assert faults.faulty("acme.guard")

    # Restarted: its health probe, or any call, gets an answer.
    hooks.disconnect("acme.guard")
    Process(hooks, "acme.guard", hung=False)
    faults.answered("acme.guard")

    verdict = await hooks.ask(PRE, CALL)
    assert verdict.allowed and not faults.faulty("acme.guard")


async def test_an_answer_starts_the_count_again(
    faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    process = Process(hooks, "acme.guard", hung=True)
    await hooks.ask(PRE, CALL)
    await hooks.ask(PRE, CALL)
    process.hung = False
    await hooks.ask(PRE, CALL)
    process.hung = True
    await hooks.ask(PRE, CALL)
    await hooks.ask(PRE, CALL)

    assert not faults.faulty("acme.guard")


def _tools_only(registry: ExtensionRegistry) -> ToolEventHook:
    """``acme.guard`` hooking nothing, so only its tools are counted."""
    registry.install(manifest("acme.guard"), state=ExtensionState.READY)
    return ToolEventHook(HookExecutor({}), session_id="s-1")


async def test_an_agent_tool_that_times_out_counts(
    registry: ExtensionRegistry, faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    ExtensionHookGate.install(hooks)
    hook = _tools_only(registry)
    timed_out = ToolResult(content="Error: Timed out while waiting for response")

    for _ in range(ExtensionFaults.LIMIT):
        await hook("mcp_ext:acme.guard_scan", lambda **kw: timed_out, {})

    assert faults.faulty("acme.guard")


async def test_a_hook_answered_between_tool_timeouts_starts_the_count_again(
    faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    """Its process answers: what times out is one slow tool, not a hung process."""
    ExtensionHookGate.install(hooks)
    hook = ToolEventHook(HookExecutor({}), session_id="s-1")
    process = Process(hooks, "acme.guard", hung=False)
    timed_out = ToolResult(content="Error: Timed out while waiting for response")

    for _ in range(ExtensionFaults.LIMIT):
        await hook("mcp_ext:acme.guard_scan", lambda **kw: timed_out, {})

    assert not faults.faulty("acme.guard")
    assert process.asked == ExtensionFaults.LIMIT


async def test_a_tool_that_reports_its_own_error_answered(
    registry: ExtensionRegistry, faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    ExtensionHookGate.install(hooks)
    hook = _tools_only(registry)
    reported = ToolResult(content="Error from MCP tool 'scan': no such file")

    for _ in range(ExtensionFaults.LIMIT + 1):
        await hook("mcp_ext:acme.guard_scan", lambda **kw: reported, {})

    assert not faults.faulty("acme.guard")


async def test_another_server_s_tool_is_nobody_s_business(
    registry: ExtensionRegistry, faults: ExtensionFaults, hooks: ExtensionHooks
) -> None:
    ExtensionHookGate.install(hooks)
    hook = _tools_only(registry)
    timed_out = ToolResult(content="Error: Timed out")

    for _ in range(ExtensionFaults.LIMIT):
        await hook("mcp_github_search", lambda **kw: timed_out, {})

    assert not faults.faulty("acme.guard")


class _Silent:
    async def call(self, method: str, args: dict[str, Any]) -> Any:
        raise ExtensionServiceError("did not answer within 60s", unanswered=True)


class _Reporting:
    async def call(self, method: str, args: dict[str, Any]) -> Any:
        raise ExtensionServiceError("'scan' reported: nope")


async def test_a_page_call_that_times_out_counts(registry: ExtensionRegistry) -> None:
    faults = ExtensionFaults(registry)
    rpc = ExtensionRpc(registry, faults=faults)
    registry.install(
        manifest("acme.guard", hooks=["PreToolUse"], rpc=["ext.acme.guard.*"]),
        state=ExtensionState.READY,
    )
    rpc.bind("acme.guard", _Silent())

    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused):
            await rpc.dispatch(caller="acme.guard", method="ext.acme.guard.scan", args={})

    assert faults.faulty("acme.guard")

    rpc.bind("acme.guard", _Reporting())
    with pytest.raises(ExtensionRefused):
        await rpc.dispatch(caller="acme.guard", method="ext.acme.guard.scan", args={})
    # A tool reporting its own error answered: the mark clears.
    assert not faults.faulty("acme.guard")


class _Sink:
    def __init__(self) -> None:
        self.kept: list[tuple[str, ExtensionNotice]] = []

    def keep_local(self, source: str, notice: ExtensionNotice) -> None:
        self.kept.append((source, notice))


def test_the_person_is_told_and_the_notice_opens_its_page(registry: ExtensionRegistry) -> None:
    sink = _Sink()

    FaultNotices(registry, sink).told("acme.guard", "acme.guard isn't responding: 3 calls")

    [(source, notice)] = sink.kept
    assert source == "acme.guard"
    assert notice.title == "acme.guard isn't responding"
    assert notice.target.page == "acme.guard.home"


# ── The restart ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
async def test_only_a_group_igni_started_can_be_claimed_and_ended() -> None:
    processes = ExtensionProcesses()
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert not await processes.claim("acme.other", "1")
        assert await processes.claim("acme.guard", str(child.pid))

        assert processes.kill("acme.guard")
        assert child.wait(timeout=5) == -signal.SIGKILL
        assert not processes.kill("acme.guard")
    finally:
        if child.poll() is None:
            child.kill()


class _Mcp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def disconnect_one(self, name: str) -> bool:
        self.calls.append(f"disconnect {name}")
        return True

    async def connect(self, name: str) -> Any:
        self.calls.append(f"connect {name}")
        return SimpleNamespace(ok=True)

    def get_client(self, name: str) -> Any:
        return None


class _Processes(ExtensionProcesses):
    def __init__(self) -> None:
        super().__init__()
        self.killed: list[str] = []

    def kill(self, extension_id: str) -> bool:
        self.killed.append(extension_id)
        return True


async def test_a_faulty_extension_s_process_is_ended_then_started_again(
    registry: ExtensionRegistry,
) -> None:
    mcp = _Mcp()
    processes = _Processes()
    health = ExtensionHealth(
        registry=registry, rpc=ExtensionRpc(registry), mcp=mcp, processes=processes
    )

    health.restart_faulty("acme.guard", "not responding")
    await asyncio.sleep(0)
    while health.pending_probes:
        await asyncio.sleep(0.01)

    assert processes.killed == ["acme.guard"]
    assert mcp.calls == ["disconnect ext:acme.guard", "connect ext:acme.guard"]
