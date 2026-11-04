"""One stream carries a run's output and every approval its sub-agents ask for.

:class:`HITLStreamMultiplexer` merges the team's Agno stream with the
sub-agent approval coordinator. Its tests covered the inline ``<think>``
splitter and the nested-relay hand-off; the merge itself — what reaches
the UI, in what order, and how a pause ends or resumes the stream — had
none. These drive :meth:`stream` with real Agno events, the real
:class:`PauseHandler` and :class:`PermissionEvaluator`, the real
:class:`PendingRequirementsStore` and the real
:class:`SubAgentHITLCoordinator`. Only the session object is a namespace
holding them, and the team is a stand-in whose ``acontinue_run`` streams
real events.

Pinned:

* content and the completion pass through, the completed run's stale
  approvals are swept, and post-run broadcasts (the plan card) drain
  after the run's content — and a broken broadcast does not end the run;
* a team stream that fails becomes an ``Error`` the UI can show, and a
  timeout says it was one;
* a sub-agent's question surfaces as a ``RunPaused`` while the team is
  still streaming, and a later reason for it as ``RequirementDetails``;
* a pause the policy leaves to the person ends the stream with
  ``RunPaused``; a mixed pause stashes what the policy decided; a pause
  the policy decided entirely resumes the run without asking.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunPausedEvent
from agno.run.requirement import RunRequirement

from ember_code.backend.hitl_stream_mux import HITLStreamMultiplexer
from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.pause_handler import PauseHandler
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_pause import PendingRequirement
from ember_code.core.config.permission_eval import PermissionEvaluator, PermissionMode
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.protocol import messages as msg


class _Bus:
    def __init__(self, fail: bool = False) -> None:
        self.drained: list[str | None] = []
        self._fail = fail

    def drain_post_run(self, run_id: str | None) -> None:
        self.drained.append(run_id)
        if self._fail:
            raise RuntimeError("bus broke")


class _Team:
    """``acontinue_run`` streams what a resumed run would."""

    def __init__(self) -> None:
        self.continued: list[dict[str, Any]] = []

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.continued.append(kwargs)
        yield RunContentEvent(content="resumed and ran it", run_id=kwargs["run_id"])
        yield RunCompletedEvent(run_id=kwargs["run_id"])


def _setup(
    evaluator: PermissionEvaluator | None = None, bus: _Bus | None = None
) -> tuple[HITLStreamMultiplexer, SimpleNamespace, PendingRequirementsStore]:
    store = PendingRequirementsStore()
    session = SimpleNamespace(
        sub_agent_hitl=SubAgentHITLCoordinator(),
        broadcast_bus=bus or _Bus(),
        main_team=_Team(),
        session_id="s-1",
    )
    mux = HITLStreamMultiplexer(
        session=session,
        store=store,
        pause_handler=PauseHandler(evaluator=evaluator, store=store),
        tracer=HITLTracer(enabled=False),
    )
    return mux, session, store


async def _events(*events: Any) -> AsyncIterator[Any]:
    for event in events:
        yield event


async def _collect(stream: AsyncIterator[msg.Message]) -> list[msg.Message]:
    return [m async for m in stream]


def _requirement(tool: str, **args: Any) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(tool_name=tool, tool_args=args, requires_confirmation=True)
    )


# ── The team's own events ─────────────────────────────────────────


async def test_content_and_completion_pass_through_and_the_run_is_tidied_up():
    bus = _Bus()
    mux, session, store = _setup(bus=bus)
    store.register(
        "stale",
        PendingRequirement(
            req=object(), run_id="r1", request=msg.HITLRequest(requirement_id="stale")
        ),
    )

    out = await _collect(
        mux.stream(
            _events(
                RunContentEvent(content="Hello", run_id="r1"),
                RunCompletedEvent(run_id="r1"),
            )
        )
    )

    assert [m.type for m in out] == ["content_delta", "run_completed"]
    assert out[0].text == "Hello"
    assert not store.has("stale")
    assert bus.drained == ["r1"]
    # The relay counted itself in and back out.
    assert session.sub_agent_hitl.listeners == 0


async def test_a_broken_post_run_broadcast_does_not_end_the_run_in_an_error():
    mux, _, _ = _setup(bus=_Bus(fail=True))

    out = await _collect(mux.stream(_events(RunCompletedEvent(run_id="r1"))))

    assert [m.type for m in out] == ["run_completed"]


async def test_a_team_stream_that_fails_becomes_an_error_the_ui_shows():
    async def failing() -> AsyncIterator[Any]:
        yield RunContentEvent(content="partial", run_id="r")
        raise RuntimeError("provider hung up")

    mux, session, _ = _setup()

    out = await _collect(mux.stream(failing()))

    assert [m.type for m in out] == ["content_delta", "error"]
    assert out[-1].text == "provider hung up"
    assert session.sub_agent_hitl.listeners == 0


async def test_a_timeout_says_the_model_took_too_long():
    async def slow() -> AsyncIterator[Any]:
        raise TimeoutError()
        yield

    mux, _, _ = _setup()

    out = await _collect(mux.stream(slow()))

    assert [m.text for m in out] == ["Request timed out — the model took too long to respond."]


# ── Sub-agent questions ───────────────────────────────────────────


async def test_a_sub_agent_s_question_surfaces_while_the_team_is_still_streaming():
    mux, session, _ = _setup()
    coordinator = session.sub_agent_hitl
    answered = asyncio.Event()
    asked: dict[str, str] = {}

    async def team() -> AsyncIterator[Any]:
        yield RunContentEvent(content="spawning the architect", run_id="r")
        asked["id"] = await coordinator.push_requirement(
            _requirement("save_file", file_name="a.py"), run_id="sub-1", agent_path=["architect"]
        )
        await answered.wait()
        coordinator.update_details(asked["id"], "It overwrites a.py")
        await asyncio.sleep(0.05)
        yield RunCompletedEvent(run_id="r")

    out: list[msg.Message] = []
    async for m in mux.stream(team()):
        out.append(m)
        if isinstance(m, msg.RunPaused):
            answered.set()

    paused = next(m for m in out if isinstance(m, msg.RunPaused))
    (request,) = paused.requirements
    assert paused.run_id == "sub-1"
    assert (request.requirement_id, request.tool_name, request.agent_path) == (
        asked["id"],
        "save_file",
        ["architect"],
    )
    assert request.tool_args == {"file_name": "a.py"}
    details = next(m for m in out if isinstance(m, msg.RequirementDetails))
    assert (details.requirement_id, details.details) == (asked["id"], "It overwrites a.py")
    assert out[-1].type == "run_completed"


def test_an_empty_batch_of_sub_agent_questions_has_no_run_id():
    paused = HITLStreamMultiplexer.build_subagent_paused([])

    assert (paused.run_id, paused.requirements) == ("", [])


# ── The team's own pause ──────────────────────────────────────────


async def test_a_pause_left_to_the_person_ends_the_stream_with_the_dialog():
    mux, _, store = _setup(PermissionEvaluator.from_strings())
    pause = RunPausedEvent(
        run_id="r", requirements=[_requirement("run_shell_command", command="ls")]
    )

    out = await _collect(
        mux.stream(_events(pause, RunContentEvent(content="never reached", run_id="r")))
    )

    (paused,) = out
    assert isinstance(paused, msg.RunPaused)
    assert paused.requirements[0].tool_name == "run_shell_command"
    assert store.has(paused.requirements[0].requirement_id)


async def test_a_mixed_pause_keeps_the_policy_s_decisions_for_the_answer():
    evaluator = PermissionEvaluator.from_strings(deny=["Bash(rm *)"])
    mux, _, store = _setup(evaluator)
    denied = _requirement("run_shell_command", command="rm -rf build")
    asked = _requirement("run_shell_command", command="make")

    out = await _collect(
        mux.stream(_events(RunPausedEvent(run_id="r", requirements=[denied, asked])))
    )

    (paused,) = out
    assert [r.tool_args for r in paused.requirements] == [{"command": "make"}]
    assert store.drain_auto_resolved("r") == [denied]
    assert denied.confirmation is False


async def test_a_pause_the_policy_decided_resumes_the_run_without_asking():
    evaluator = PermissionEvaluator.from_strings(mode=PermissionMode.BYPASS_PERMISSIONS)
    mux, session, _ = _setup(evaluator)
    req = _requirement("run_shell_command", command="make")

    out = await _collect(mux.stream(_events(RunPausedEvent(run_id="r", requirements=[req]))))

    assert [m.type for m in out] == ["content_delta", "run_completed"]
    assert out[0].text == "resumed and ran it"
    (call,) = session.main_team.continued
    assert (call["run_id"], call["session_id"], call["requirements"]) == ("r", "s-1", [req])
    assert not any(isinstance(m, msg.RunPaused) for m in out)


async def test_a_pause_with_nothing_to_decide_ends_quietly():
    mux, session, _ = _setup(PermissionEvaluator.from_strings())

    out = await _collect(mux.stream(_events(RunPausedEvent(run_id="r", requirements=[]))))

    assert out == []
    assert session.main_team.continued == []
