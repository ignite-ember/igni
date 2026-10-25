"""An extension stops, or changes, what igni is about to do.

``docs/HOST_OBJECT_MODEL.md``, *Intercepting*: igni asks every extension
that declared an event (``permissions.intercepts``), in the super admin's
priority order, before it happens; a reject ends it, a change is what the next
is handed, and a silence is decided by the event, never the extension.
These drive :class:`Interceptors` with stand-in processes, the places igni
fires the events — the tool hook, the plan tool, a message entering, every
write — and the real loopback socket.
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
from websockets.asyncio.client import connect

from ember_code.backend.message_dispatcher import ChatSubmissions
from ember_code.core.gates import EventGate, GateVerdict
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.tools.plan.store import PlanStore
from ember_code.core.tools.plan.submission import PlanTransactionCoordinator
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.intercepts import PERSON, Interceptors
from ember_code.extensions.host.wire import InterceptAnswer, Op, Request
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig, manifest

CALL = {"session_id": "s-1", "tool": "run_shell_command", "args": {"command": "ls"}}

Answering = Callable[[dict[str, Any]], dict[str, Any] | None]


class StandIn:
    """An extension's process: answers each intercept it is sent with ``answer``.

    ``answer`` returns the answer's fields (``outcome``, ``reason``,
    ``value``), or ``None`` to stay silent.
    """

    def __init__(self, interceptors: Interceptors, extension_id: str, answer: Answering) -> None:
        self.interceptors = interceptors
        self.extension_id = extension_id
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any]]] = []
        interceptors.connect(extension_id, self.send)

    def send(self, frame: str) -> None:
        message = json.loads(frame)
        self.asked.append((message["event"], message["value"]))
        fields = self.answer(message["value"])
        if fields is None:
            return
        asyncio.get_running_loop().call_soon(
            self.interceptors.answered,
            self.extension_id,
            InterceptAnswer(intercept=message["id"], **fields),
        )


def install(
    registry: ExtensionRegistry,
    extension_id: str,
    *intercepts: dict[str, Any],
    host: tuple[str, ...] = ("chat.read",),
    state: ExtensionState = ExtensionState.READY,
) -> None:
    registry.install(
        manifest(extension_id, host=list(host), intercepts=list(intercepts)), state=state
    )


@pytest.fixture
def registry() -> ExtensionRegistry:
    return ExtensionRegistry()


@pytest.fixture
def interceptors(registry: ExtensionRegistry) -> Interceptors:
    found = Interceptors(timeout=0.2)
    found.attach(registry)
    return found


def allow(_: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": "allow"}


# ── The chain ─────────────────────────────────────────────────────


async def test_nobody_intercepting_lets_it_happen_as_it_is(interceptors: Interceptors) -> None:
    verdict = await interceptors.run("tools.calling", CALL)
    assert verdict.allowed and verdict.value == CALL


async def test_a_reject_ends_the_chain_with_its_reason(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"})
    install(registry, "acme.b", {"event": "tools.calling"})
    a = StandIn(interceptors, "acme.a", lambda _: {"outcome": "reject", "reason": "No shell."})
    b = StandIn(interceptors, "acme.b", allow)

    verdict = await interceptors.run("tools.calling", CALL)

    assert not verdict.allowed
    assert verdict.by == "acme.a"
    assert verdict.reason == "No shell. (the extension acme.a)"
    assert len(a.asked) == 1 and b.asked == []


async def test_changes_chain_in_priority_order(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling", "modify": True})
    install(registry, "acme.b", {"event": "tools.calling", "modify": True})
    interceptors.set_order(["acme.b", "acme.a"])

    def add(key: str) -> Answering:
        def answer(value: dict[str, Any]) -> dict[str, Any]:
            return {"outcome": "modify", "value": {**value, "args": {**value["args"], key: True}}}

        return answer

    b = StandIn(interceptors, "acme.b", add("b"))
    a = StandIn(interceptors, "acme.a", add("a"))

    verdict = await interceptors.run("tools.calling", CALL)

    assert verdict.allowed
    assert verdict.value["args"] == {"command": "ls", "b": True, "a": True}
    # b went first, so a was handed b's change.
    assert a.asked[0][1]["args"] == {"command": "ls", "b": True}
    assert b.asked[0][1]["args"] == {"command": "ls"}


async def test_a_change_from_one_that_may_not_change_it_is_not_taken(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"})
    StandIn(interceptors, "acme.a", lambda v: {"outcome": "modify", "value": {**v, "args": {}}})

    verdict = await interceptors.run("tools.calling", CALL)

    assert verdict.allowed and verdict.value == CALL


async def test_changing_what_cannot_be_changed_is_a_failure(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling", "modify": True})
    StandIn(interceptors, "acme.a", lambda v: {"outcome": "modify", "value": {**v, "tool": "rm"}})

    verdict = await interceptors.run("tools.calling", CALL)

    # tools.calling fails closed: a call nobody could check does not run.
    assert not verdict.allowed
    assert "tool, which cannot be changed" in verdict.reason


async def test_a_change_that_does_not_fit_the_event_is_a_failure(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling", "modify": True})
    StandIn(interceptors, "acme.a", lambda v: {"outcome": "modify", "value": {**v, "args": "x"}})

    verdict = await interceptors.run("tools.calling", CALL)

    assert not verdict.allowed and "does not fit ToolCalling" in verdict.reason


@pytest.mark.parametrize(
    ("event", "value", "goes_ahead"),
    [
        ("tools.calling", CALL, False),
        ("chat.submitting", {"session_id": "s", "text": "hi", "has_media": False}, False),
        ("plan.submitting", {"session_id": "s", "plan": "p", "tasks": [], "attempt": 0}, True),
        ("ui.navigate", {"path": "ui.navigate", "writer": PERSON}, True),
    ],
)
async def test_a_silence_is_decided_by_the_event(
    registry: ExtensionRegistry,
    interceptors: Interceptors,
    event: str,
    value: dict[str, Any],
    goes_ahead: bool,
) -> None:
    install(registry, "acme.a", {"event": event}, host=("chat.read", "ui"))
    StandIn(interceptors, "acme.a", lambda _: None)

    verdict = await interceptors.run(event, value)

    assert verdict.allowed is goes_ahead
    if not goes_ahead:
        assert verdict.by == "acme.a" and "no answer within" in verdict.reason


async def test_a_process_that_is_not_connected_is_a_silence(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"})

    verdict = await interceptors.run("tools.calling", CALL)

    assert not verdict.allowed and "not connected" in verdict.reason


async def test_a_handler_that_failed_is_a_silence(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"})
    StandIn(interceptors, "acme.a", lambda _: {"outcome": "failed", "reason": "boom"})

    verdict = await interceptors.run("tools.calling", CALL)

    assert not verdict.allowed and "it failed: boom" in verdict.reason


async def test_a_disabled_extension_is_not_asked(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"}, state=ExtensionState.DISABLED)

    verdict = await interceptors.run("tools.calling", CALL)

    assert verdict.allowed


async def test_an_interceptor_without_the_grant_is_not_asked(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"}, host=())
    stand_in = StandIn(interceptors, "acme.a", lambda _: {"outcome": "reject", "reason": "no"})

    verdict = await interceptors.run("tools.calling", CALL)

    assert verdict.allowed and stand_in.asked == []


async def test_an_extension_is_not_asked_about_its_own_write(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "ui.navigate"}, host=("ui",))
    stand_in = StandIn(interceptors, "acme.a", lambda _: {"outcome": "reject", "reason": "no"})

    verdict = await interceptors.run(
        "ui.navigate", {"path": "ui.navigate", "writer": "acme.a"}, writer="acme.a"
    )

    assert verdict.allowed and stand_in.asked == []


async def test_a_reject_with_no_reason_is_not_an_answer(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "tools.calling"})
    StandIn(interceptors, "acme.a", lambda _: {"outcome": "reject", "reason": "  "})

    verdict = await interceptors.run("tools.calling", CALL)

    assert not verdict.allowed and "without saying why" in verdict.reason


def test_every_write_and_decision_point_can_be_intercepted_and_reads_cannot() -> None:
    names = set(HOST_SCHEMA.intercept_names())
    assert {"chat.submitting", "tools.calling", "plan.submitting"} <= names
    assert {"ui.navigate", "sessions.current", "chat.post", "plugins.install"} <= names
    # Reads, and an extension's own state, are nobody's to intercept.
    assert not names & {"chat.search", "workspace.read", "decide", "app.state.set", "self.settings"}


# ── Where igni asks ───────────────────────────────────────────────


class Gate:
    """The core's gate, answered by the test."""

    def __init__(self, answer: Callable[[str, dict[str, Any]], GateVerdict]) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any]]] = []
        EventGate.install(self.ask)

    async def ask(self, event: str, value: dict[str, Any]) -> GateVerdict:
        self.asked.append((event, value))
        return self.answer(event, value)


def _hook() -> ToolEventHook:
    return ToolEventHook(HookExecutor({}), session_id="s-1")


async def test_a_rejected_tool_call_does_not_run_and_the_agent_is_told_why() -> None:
    Gate(lambda e, v: GateVerdict(allowed=False, value=v, reason="Not on Fridays."))
    ran: list[dict[str, Any]] = []

    result = await _hook()("probe", lambda **kw: ran.append(kw) or "ran", {"x": 1})

    assert result == "Blocked: Not on Fridays."
    assert ran == []


async def test_a_changed_tool_call_runs_with_what_it_was_changed_to() -> None:
    gate = Gate(lambda e, v: GateVerdict(allowed=True, value={**v, "args": {"x": 2}}))
    ran: list[dict[str, Any]] = []

    result = await _hook()("probe", lambda **kw: ran.append(kw) or "ran", {"x": 1})

    assert result == "ran" and ran == [{"x": 2}]
    assert gate.asked == [
        ("tools.calling", {"session_id": "s-1", "tool": "probe", "args": {"x": 1}})
    ]


def _plan_session() -> MagicMock:
    session = MagicMock()
    session.session_id = "s-1"
    session.plan_store = PlanStore()
    return session


async def test_a_rejected_plan_goes_back_to_the_agent_and_counts_as_an_attempt() -> None:
    gate = Gate(lambda e, v: GateVerdict(allowed=False, value=v, reason="Cite two files."))
    session = _plan_session()
    coordinator = PlanTransactionCoordinator(session)

    first = await coordinator.submit(plan="Do it.", tasks=None)
    second = await coordinator.submit(plan="Do it.", tasks=None)

    assert not first.ok and first.reply_text == "Plan rejected: Cite two files."
    assert session.plan_store.latest == ""
    assert [v["attempt"] for _, v in gate.asked] == [0, 1]
    assert second.reply_text.startswith("Plan rejected")


async def test_a_changed_plan_is_the_one_the_person_sees() -> None:
    Gate(lambda e, v: GateVerdict(allowed=True, value={**v, "plan": v["plan"] + "\n\nChecked."}))
    session = _plan_session()

    result = await PlanTransactionCoordinator(session).submit(plan="Do it.", tasks=None)

    assert result.ok
    assert session.plan_store.latest == "Do it.\n\nChecked."


async def test_entering_plan_mode_starts_the_attempts_again() -> None:
    session = _plan_session()
    session.set_permission_mode = MagicMock(return_value="plan")
    session.plan_store.rejected = 2

    await PlanTransactionCoordinator(session).enter(reason="", task="")

    assert session.plan_store.rejected == 0


async def test_a_message_is_asked_about_before_it_is_wrapped(
    registry: ExtensionRegistry, interceptors: Interceptors
) -> None:
    install(registry, "acme.a", {"event": "chat.submitting", "modify": True})
    stand_in = StandIn(
        interceptors,
        "acme.a",
        lambda v: {"outcome": "modify", "value": {**v, "text": v["text"].replace("pw=1", "pw=…")}},
    )

    verdict = await ChatSubmissions(interceptors).check("s-1", "login pw=1", has_media=False)

    assert verdict.allowed and verdict.value["text"] == "login pw=…"
    assert stand_in.asked[0][1] == {
        "session_id": "s-1",
        "text": "login pw=1",
        "from_extension": None,
        "has_media": False,
    }


# ── Writes, and the socket ────────────────────────────────────────


@pytest.fixture
async def rig(tmp_path: Path) -> Any:
    found = await HostRig(tmp_path).start(strict=False)
    try:
        yield found
    finally:
        await found.stop()


def _rejecting(rig: HostRig, extension_id: str, reason: str) -> StandIn:
    assert rig.model is not None
    return StandIn(
        rig.model.interceptors, extension_id, lambda _: {"outcome": "reject", "reason": reason}
    )


async def test_an_extension_s_write_another_extension_rejects_is_refused(rig: HostRig) -> None:
    rig.install("acme.guard", host=["ui"], intercepts=[{"event": "ui.navigate"}])
    rig.install("acme.writer", host=["ui"])
    await rig.window()
    guard = _rejecting(rig, "acme.guard", "Stay on the chat.")

    reply = await rig.ask("acme.writer", "call", "ui.navigate", page="plugins")

    assert not reply.ok and reply.error is not None
    assert reply.error.code == "refused"
    assert reply.error.message == "Stay on the chat. (the extension acme.guard)"
    [(event, value)] = guard.asked
    assert event == "ui.navigate" and value["writer"] == "acme.writer"
    assert value["params"]["page"] == "plugins"


async def test_a_window_s_own_write_an_extension_rejects_is_not_made(rig: HostRig) -> None:
    rig.install("acme.guard", host=["ui"], intercepts=[{"event": "ui.panel"}])
    window = await rig.window()
    _rejecting(rig, "acme.guard", "Not now.")

    answer = await rig.window_writes(window, "panel", {"kind": "search"})

    assert isinstance(answer, str) and "refused: Not now." in answer


async def test_the_person_s_write_an_extension_rejects_is_not_made(rig: HostRig) -> None:
    rig.install("acme.guard", host=["sessions"], intercepts=[{"event": "sessions.open"}])
    _rejecting(rig, "acme.guard", "This project only.")
    assert rig.orchestrator is not None

    await rig.orchestrator.dispatch(msg.SessionSwitch(id="r-1", session_id="other"))

    [error] = [m for m in rig.windows.of(msg.Error) if m.id == "r-1"]
    assert error.text == "This project only. (the extension acme.guard)"


async def test_a_page_cannot_answer_an_intercept(rig: HostRig) -> None:
    rig.install("acme.guard", host=["ui"])
    assert rig.model is not None
    reply = await rig.model.handle(
        "acme.guard", Request(id=1, op=Op.ANSWER, params={"intercept": "i-1", "outcome": "allow"})
    )
    assert not reply.ok and reply.error is not None and reply.error.code == "invalid"


async def test_a_process_answers_an_intercept_over_its_socket(rig: HostRig) -> None:
    rig.install("acme.guard", host=["chat.read"], intercepts=[{"event": "tools.calling"}])
    assert rig.channel is not None and rig.model is not None
    env = await rig.channel.grant("acme.guard", secrets.token_urlsafe(16))

    async with connect(
        env["IGNI_HOST_URL"],
        additional_headers={"Authorization": f"Bearer {env['IGNI_HOST_SECRET']}"},
    ) as socket:
        hello = json.loads(await socket.recv())
        assert hello["op"] == "hello"
        asking = asyncio.create_task(rig.model.interceptors.run("tools.calling", CALL))
        frame = json.loads(await socket.recv())
        assert frame["op"] == "intercept" and frame["event"] == "tools.calling"
        assert frame["value"] == CALL
        await socket.send(
            json.dumps(
                {
                    "id": 7,
                    "op": "answer",
                    "params": {"intercept": frame["id"], "outcome": "reject", "reason": "No."},
                }
            )
        )
        reply = json.loads(await socket.recv())
        assert reply == {"op": "reply", "id": 7, "ok": True, "result": None, "error": None}
        verdict = await asking

    assert not verdict.allowed and verdict.reason == "No. (the extension acme.guard)"


async def test_a_process_that_goes_away_has_failed_what_it_owed(rig: HostRig) -> None:
    rig.install("acme.guard", host=["chat.read"], intercepts=[{"event": "tools.calling"}])
    assert rig.channel is not None and rig.model is not None
    env = await rig.channel.grant("acme.guard", secrets.token_urlsafe(16))

    async with connect(
        env["IGNI_HOST_URL"],
        additional_headers={"Authorization": f"Bearer {env['IGNI_HOST_SECRET']}"},
    ) as socket:
        await socket.recv()
        asking = asyncio.create_task(rig.model.interceptors.run("tools.calling", CALL))
        await socket.recv()
    verdict = await asking

    assert not verdict.allowed and "went away" in verdict.reason
