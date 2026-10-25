"""An extension answers igni's hook events.

``docs/HOOKS.md``, *Extensions*: an extension lists hook events in
``permissions.hooks`` and its process answers them. On the five that
decide (``DECIDING``) it is asked, in the super admin's priority order,
before igni's own configured hooks: a reject ends it, a change is what
the next one — and then the configured hooks — are handed, and a
silence is decided by the event, never the extension. Every other event
is told, and nothing waits. These drive :class:`ExtensionHooks` with
stand-in processes, the places igni fires the events, and the real
loopback socket.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from websockets.asyncio.client import connect

from ember_code.backend.run_hook_gate import RunHookGate
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.extension_gate import (
    DECIDING,
    ExtensionHookGate,
    ExtensionHookSpec,
    ExtensionHookVerdict,
    HookFailMode,
)
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.tools.plan.store import PlanStore
from ember_code.core.tools.plan.submission import PlanTransactionCoordinator
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.wire import HookAnswer, Op, Request
from ember_code.extensions.impact import ExtensionImpact, ImpactHook
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from tests.host_model_helpers import HostRig, manifest

CALL = {"session_id": "s-1", "tool_name": "run_shell_command", "tool_args": {"command": "ls"}}
PRE = DECIDING[HookEvent.PRE_TOOL_USE]

Answering = Callable[[dict[str, Any]], dict[str, Any] | None]


class StandIn:
    """An extension's process: answers each hook event it is sent with ``answer``.

    ``answer`` returns the answer's fields (``outcome``, ``reason``,
    ``value``), or ``None`` to stay silent. A told event is kept in
    ``told`` and never answered.
    """

    def __init__(self, hooks: ExtensionHooks, extension_id: str, answer: Answering) -> None:
        self.hooks = hooks
        self.extension_id = extension_id
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.told: list[tuple[str, dict[str, Any]]] = []
        hooks.connect(extension_id, self.send)

    def send(self, frame: str) -> None:
        message = json.loads(frame)
        if message["id"] is None:
            self.told.append((message["event"], message["value"]))
            return
        self.asked.append((message["event"], message["value"]))
        fields = self.answer(message["value"])
        if fields is None:
            return
        asyncio.get_running_loop().call_soon(
            self.hooks.answered,
            self.extension_id,
            HookAnswer(hook=message["id"], **fields),
        )


def install(
    registry: ExtensionRegistry,
    extension_id: str,
    *events: str,
    state: ExtensionState = ExtensionState.READY,
) -> None:
    registry.install(manifest(extension_id, hooks=list(events)), state=state)


@pytest.fixture
def registry() -> ExtensionRegistry:
    return ExtensionRegistry()


@pytest.fixture
def hooks(registry: ExtensionRegistry) -> ExtensionHooks:
    found = ExtensionHooks(timeout=0.2)
    found.attach(registry)
    return found


def allow(_: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": "allow"}


# ── The chain ─────────────────────────────────────────────────────


async def test_nobody_hooking_lets_it_happen_as_it_is(hooks: ExtensionHooks) -> None:
    verdict = await hooks.ask(PRE, CALL)
    assert verdict.allowed and verdict.value == CALL


async def test_a_reject_ends_the_chain_with_its_reason(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    install(registry, "acme.b", "PreToolUse")
    a = StandIn(hooks, "acme.a", lambda _: {"outcome": "reject", "reason": "No shell."})
    b = StandIn(hooks, "acme.b", allow)

    verdict = await hooks.ask(PRE, CALL)

    assert not verdict.allowed
    assert verdict.by == "acme.a"
    assert verdict.reason == "No shell. (the extension acme.a)"
    assert len(a.asked) == 1 and b.asked == []


async def test_changes_chain_in_priority_order(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    install(registry, "acme.b", "PreToolUse")
    hooks.set_priority(["acme.b", "acme.a"])

    def add(key: str) -> Answering:
        def answer(value: dict[str, Any]) -> dict[str, Any]:
            args = {**value["tool_args"], key: True}
            return {"outcome": "modify", "value": {**value, "tool_args": args}}

        return answer

    b = StandIn(hooks, "acme.b", add("b"))
    a = StandIn(hooks, "acme.a", add("a"))

    verdict = await hooks.ask(PRE, CALL)

    assert verdict.allowed
    assert verdict.value["tool_args"] == {"command": "ls", "b": True, "a": True}
    # b went first, so a was handed b's change.
    assert a.asked[0][1]["tool_args"] == {"command": "ls", "b": True}
    assert b.asked[0][1]["tool_args"] == {"command": "ls"}


async def test_changing_what_cannot_be_changed_is_a_failure(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    StandIn(hooks, "acme.a", lambda v: {"outcome": "modify", "value": {**v, "tool_name": "rm"}})

    verdict = await hooks.ask(PRE, CALL)

    # PreToolUse fails closed: a call nobody could check does not run.
    assert not verdict.allowed
    assert "tool_name, which cannot be changed" in verdict.reason


async def test_a_change_that_does_not_fit_the_event_is_a_failure(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    StandIn(hooks, "acme.a", lambda v: {"outcome": "modify", "value": {**v, "tool_args": "x"}})

    verdict = await hooks.ask(PRE, CALL)

    assert not verdict.allowed and "does not fit PreToolUse" in verdict.reason


async def test_changing_an_event_with_nothing_to_change_is_a_failure(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "Stop")
    StandIn(hooks, "acme.a", lambda v: {"outcome": "modify", "value": v})

    verdict = await hooks.ask(DECIDING[HookEvent.STOP], {"session_id": "s", "response": ""})

    # Stop fails open: the failure is logged and the agent stops.
    assert verdict.allowed


@pytest.mark.parametrize(
    ("event", "value", "goes_ahead"),
    [
        (HookEvent.PRE_TOOL_USE, CALL, False),
        (HookEvent.USER_PROMPT_SUBMIT, {"session_id": "s", "message": "hi"}, False),
        (
            HookEvent.POST_TOOL_USE,
            {"session_id": "s", "tool_name": "t", "tool_args": {}, "result": "r"},
            True,
        ),
        (HookEvent.STOP, {"session_id": "s", "response": ""}, True),
        (HookEvent.PLAN_SUBMIT, {"session_id": "s", "plan": "p", "tasks": [], "attempt": 0}, True),
    ],
)
async def test_a_silence_is_decided_by_the_event(
    registry: ExtensionRegistry,
    hooks: ExtensionHooks,
    event: HookEvent,
    value: dict[str, Any],
    goes_ahead: bool,
) -> None:
    install(registry, "acme.a", event.value)
    StandIn(hooks, "acme.a", lambda _: None)

    verdict = await hooks.ask(DECIDING[event], value)

    assert verdict.allowed is goes_ahead
    if not goes_ahead:
        assert verdict.by == "acme.a" and "no answer within" in verdict.reason


def test_only_a_tool_call_and_a_message_fail_closed() -> None:
    closed = {event for event, spec in DECIDING.items() if spec.fails is HookFailMode.CLOSED}
    assert closed == {HookEvent.PRE_TOOL_USE, HookEvent.USER_PROMPT_SUBMIT}


async def test_a_process_that_is_not_connected_is_a_silence(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")

    verdict = await hooks.ask(PRE, CALL)

    assert not verdict.allowed and "not connected" in verdict.reason


async def test_a_process_that_failed_to_start_is_refused_at_once_with_why(
    registry: ExtensionRegistry,
) -> None:
    registry.install(
        manifest("acme.a", hooks=["PreToolUse"]),
        state=ExtensionState.FAILED,
        reason="its environment would not install",
    )
    slow = ExtensionHooks(timeout=30)
    slow.attach(registry)

    verdict = await asyncio.wait_for(slow.ask(PRE, CALL), 1)

    assert not verdict.allowed
    assert "not running: its environment would not install" in verdict.reason


async def test_a_handler_that_failed_is_a_silence(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    StandIn(hooks, "acme.a", lambda _: {"outcome": "failed", "reason": "boom"})

    verdict = await hooks.ask(PRE, CALL)

    assert not verdict.allowed and "it failed: boom" in verdict.reason


async def test_a_disabled_extension_is_not_asked(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse", state=ExtensionState.DISABLED)

    verdict = await hooks.ask(PRE, CALL)

    assert verdict.allowed


async def test_a_reject_with_no_reason_is_not_an_answer(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    StandIn(hooks, "acme.a", lambda _: {"outcome": "reject", "reason": "  "})

    verdict = await hooks.ask(PRE, CALL)

    assert not verdict.allowed and "without saying why" in verdict.reason


async def test_an_event_that_decides_nothing_is_told_and_nobody_waits(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "SessionStart")
    install(registry, "acme.b", "PreToolUse")
    a = StandIn(hooks, "acme.a", lambda _: pytest.fail("a told event is not answered"))
    b = StandIn(hooks, "acme.b", allow)
    ExtensionHookGate.install(hooks)

    await HookExecutor({}).execute("SessionStart", {"session_id": "s-1", "source": "startup"})

    assert a.told == [("SessionStart", {"session_id": "s-1", "source": "startup"})]
    assert b.told == []


async def test_a_deciding_event_is_never_told_from_the_executor(
    registry: ExtensionRegistry, hooks: ExtensionHooks
) -> None:
    install(registry, "acme.a", "PreToolUse")
    a = StandIn(hooks, "acme.a", allow)
    ExtensionHookGate.install(hooks)

    await HookExecutor({}).execute("PreToolUse", {"session_id": "s-1", "tool_name": "x"})

    assert a.told == [] and a.asked == []


# ── Where igni asks ───────────────────────────────────────────────


class Gate:
    """The core's gate, answered by the test."""

    def __init__(self, answer: Callable[[HookEvent, dict[str, Any]], ExtensionHookVerdict]) -> None:
        self.answer = answer
        self.asked: list[tuple[HookEvent, dict[str, Any]]] = []
        ExtensionHookGate.install(self)

    def hooks(self, event: HookEvent) -> bool:
        return True

    async def ask(self, spec: ExtensionHookSpec, value: dict[str, Any]) -> ExtensionHookVerdict:
        self.asked.append((spec.event, value))
        return self.answer(spec.event, value)

    def tell(self, event: str, payload: dict[str, Any]) -> None:
        return None

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        return None


def _go(_: HookEvent, value: dict[str, Any]) -> ExtensionHookVerdict:
    return ExtensionHookVerdict(allowed=True, value=value)


class Tool:
    """A tool that records the arguments it ran with."""

    def __init__(self) -> None:
        self.ran: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.ran.append(kwargs)
        return "ran"


def _hook(executor: HookExecutor | None = None) -> ToolEventHook:
    return ToolEventHook(executor or HookExecutor({}), session_id="s-1")


async def test_a_rejected_tool_call_does_not_run_and_the_agent_is_told_why() -> None:
    Gate(lambda e, v: ExtensionHookVerdict(allowed=False, value=v, reason="Not on Fridays."))
    tool = Tool()

    result = await _hook()("probe", tool, {"x": 1})

    assert result == "Blocked: Not on Fridays."
    assert tool.ran == []


async def test_a_changed_tool_call_runs_with_what_it_was_changed_to() -> None:
    def change(event: HookEvent, value: dict[str, Any]) -> ExtensionHookVerdict:
        if event is HookEvent.PRE_TOOL_USE:
            return ExtensionHookVerdict(allowed=True, value={**value, "tool_args": {"x": 2}})
        return _go(event, value)

    gate = Gate(change)
    tool = Tool()

    result = await _hook()("probe", tool, {"x": 1})

    assert result == "ran" and tool.ran == [{"x": 2}]
    assert gate.asked[0] == (
        HookEvent.PRE_TOOL_USE,
        {"session_id": "s-1", "tool_name": "probe", "tool_args": {"x": 1}},
    )


async def test_the_configured_pre_tool_use_hooks_see_the_extensions_change(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    executor = HookExecutor(
        {
            "PreToolUse": [
                HookDefinition(type="command", command=f"cat > {seen}; echo '{{}}'", timeout=5000)
            ]
        }
    )
    Gate(
        lambda e, v: (
            ExtensionHookVerdict(allowed=True, value={**v, "tool_args": {"x": 2}})
            if e is HookEvent.PRE_TOOL_USE
            else _go(e, v)
        )
    )

    await _hook(executor)("probe", lambda **kw: "ran", {"x": 1})

    assert json.loads(seen.read_text())["tool_args"] == {"x": "2"}


async def test_a_withheld_result_never_reaches_the_model() -> None:
    Gate(
        lambda e, v: (
            ExtensionHookVerdict(allowed=False, value=v, reason="It holds a secret.")
            if e is HookEvent.POST_TOOL_USE
            else _go(e, v)
        )
    )

    result = await _hook()("probe", lambda **kw: "token=abc", {})

    assert result == "Withheld: It holds a secret."


async def test_a_changed_result_is_what_the_model_reads() -> None:
    Gate(
        lambda e, v: (
            ExtensionHookVerdict(
                allowed=True, value={**v, "result": v["result"].replace("abc", "…")}
            )
            if e is HookEvent.POST_TOOL_USE
            else _go(e, v)
        )
    )

    result = await _hook()("probe", lambda **kw: "token=abc", {})

    assert result == "token=…"


async def test_a_result_that_is_not_text_is_not_changed() -> None:
    Gate(
        lambda e, v: (
            ExtensionHookVerdict(allowed=True, value={**v, "result": "changed"})
            if e is HookEvent.POST_TOOL_USE
            else _go(e, v)
        )
    )

    result = await _hook()("probe", lambda **kw: {"rows": 3}, {})

    assert result == {"rows": 3}


async def test_a_message_an_extension_changes_is_the_one_run() -> None:
    gate = Gate(
        lambda e, v: ExtensionHookVerdict(
            allowed=True, value={**v, "message": v["message"].replace("pw=1", "pw=…")}
        )
    )

    result = await RunHookGate(HookExecutor({}), "s-1").fire_user_prompt_submit("login pw=1")

    assert result.should_continue and result.text == "login pw=…"
    assert gate.asked == [
        (HookEvent.USER_PROMPT_SUBMIT, {"session_id": "s-1", "message": "login pw=1"})
    ]


async def test_a_message_an_extension_rejects_does_not_start_a_run() -> None:
    Gate(lambda e, v: ExtensionHookVerdict(allowed=False, value=v, reason="No secrets."))

    result = await RunHookGate(HookExecutor({}), "s-1").fire_user_prompt_submit("pw=1")

    assert not result.should_continue and result.block_message == "No secrets."


async def test_a_stop_an_extension_rejects_is_a_stop_hook_block() -> None:
    Gate(lambda e, v: ExtensionHookVerdict(allowed=False, value=v, reason="Run the tests."))

    result = await RunHookGate(HookExecutor({}), "s-1").fire_stop()

    assert not result.should_continue and result.block_message == "Run the tests."


def _plan_session(executor: HookExecutor | None = None) -> MagicMock:
    session = MagicMock()
    session.session_id = "s-1"
    session.plan_store = PlanStore()
    session.hook_executor = executor or HookExecutor({})
    return session


async def test_a_rejected_plan_goes_back_to_the_agent_and_counts_as_an_attempt() -> None:
    gate = Gate(lambda e, v: ExtensionHookVerdict(allowed=False, value=v, reason="Cite two files."))
    session = _plan_session()
    coordinator = PlanTransactionCoordinator(session)

    first = await coordinator.submit(plan="Do it.", tasks=None)
    second = await coordinator.submit(plan="Do it.", tasks=None)

    assert not first.ok and first.reply_text == "Plan rejected: Cite two files."
    assert session.plan_store.latest == ""
    assert [e for e, _ in gate.asked] == [HookEvent.PLAN_SUBMIT, HookEvent.PLAN_SUBMIT]
    assert [v["attempt"] for _, v in gate.asked] == [0, 1]
    assert second.reply_text.startswith("Plan rejected")


async def test_a_changed_plan_is_the_one_the_person_sees() -> None:
    Gate(
        lambda e, v: ExtensionHookVerdict(
            allowed=True, value={**v, "plan": v["plan"] + "\n\nChecked."}
        )
    )
    session = _plan_session()

    result = await PlanTransactionCoordinator(session).submit(plan="Do it.", tasks=None)

    assert result.ok
    assert session.plan_store.latest == "Do it.\n\nChecked."


async def test_a_configured_plan_submit_hook_can_reject_a_plan_too() -> None:
    executor = HookExecutor(
        {
            "PlanSubmit": [
                HookDefinition(
                    type="command",
                    command="""echo '{"continue": false, "systemMessage": "Too short."}'""",
                    timeout=5000,
                )
            ]
        }
    )
    session = _plan_session(executor)

    result = await PlanTransactionCoordinator(session).submit(plan="Do it.", tasks=None)

    assert not result.ok and result.reply_text == "Plan rejected: Too short."
    assert session.plan_store.rejected == 1


async def test_entering_plan_mode_starts_the_attempts_again() -> None:
    session = _plan_session()
    session.set_permission_mode = MagicMock(return_value="plan")
    session.plan_store.rejected = 2

    await PlanTransactionCoordinator(session).enter(reason="", task="")

    assert session.plan_store.rejected == 0


# ── The manifest and the impact report ────────────────────────────


def test_an_endpoint_cannot_hook_anything() -> None:
    with pytest.raises(ValidationError, match="permissions.hooks"):
        ExtensionManifest.model_validate(
            {
                "id": "acme.remote",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "name": "Remote",
                "trust": "unsigned",
                "endpoint": {"url": "https://remote.example.com/mcp"},
                "permissions": {"hooks": ["PreToolUse"]},
            }
        )


def test_an_unknown_event_is_refused() -> None:
    with pytest.raises(ValidationError):
        manifest("acme.a", hooks=["tools.calling"])


def test_the_impact_report_says_which_hooks_decide() -> None:
    impact = ExtensionImpact.of(manifest("acme.a", hooks=["SessionStart", "PreToolUse"]))
    assert impact.hooks == [
        ImpactHook(event="PreToolUse", decides=True),
        ImpactHook(event="SessionStart", decides=False),
    ]


# ── The socket ────────────────────────────────────────────────────


@pytest.fixture
async def rig(tmp_path: Path) -> Any:
    found = await HostRig(tmp_path).start(strict=False)
    try:
        yield found
    finally:
        await found.stop()


async def test_a_page_cannot_answer_a_hook(rig: HostRig) -> None:
    rig.install("acme.guard", host=["ui"])
    assert rig.model is not None
    reply = await rig.model.handle(
        "acme.guard", Request(id=1, op=Op.ANSWER, params={"hook": "h-1", "outcome": "allow"})
    )
    assert not reply.ok and reply.error is not None and reply.error.code == "invalid"


async def test_a_process_answers_a_hook_over_its_socket(rig: HostRig) -> None:
    rig.install("acme.guard", hooks=["PreToolUse"])
    assert rig.channel is not None
    env = await rig.channel.grant("acme.guard", secrets.token_urlsafe(16))

    async with connect(
        env["IGNI_HOST_URL"],
        additional_headers={"Authorization": f"Bearer {env['IGNI_HOST_SECRET']}"},
    ) as socket:
        hello = json.loads(await socket.recv())
        assert hello["op"] == "hello"
        asking = asyncio.create_task(ExtensionHooks.shared().ask(PRE, CALL))
        frame = json.loads(await socket.recv())
        assert frame["op"] == "hook" and frame["event"] == "PreToolUse"
        assert frame["value"] == CALL
        await socket.send(
            json.dumps(
                {
                    "id": 7,
                    "op": "answer",
                    "params": {"hook": frame["id"], "outcome": "reject", "reason": "No."},
                }
            )
        )
        reply = json.loads(await socket.recv())
        assert reply == {"op": "reply", "id": 7, "ok": True, "result": None, "error": None}
        verdict = await asking

    assert not verdict.allowed and verdict.reason == "No. (the extension acme.guard)"


async def test_a_process_that_goes_away_has_failed_what_it_owed(rig: HostRig) -> None:
    rig.install("acme.guard", hooks=["PreToolUse"])
    assert rig.channel is not None
    env = await rig.channel.grant("acme.guard", secrets.token_urlsafe(16))

    async with connect(
        env["IGNI_HOST_URL"],
        additional_headers={"Authorization": f"Bearer {env['IGNI_HOST_SECRET']}"},
    ) as socket:
        await socket.recv()
        asking = asyncio.create_task(ExtensionHooks.shared().ask(PRE, CALL))
        await socket.recv()
    verdict = await asking

    assert not verdict.allowed and "went away" in verdict.reason
