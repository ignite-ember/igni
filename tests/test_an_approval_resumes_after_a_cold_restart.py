"""A run waiting on an approval survives the backend, and finishes when answered.

``docs/CLOUD.md`` section 9. Approvals have no expiry: the person may
answer after the producer's VM was lost, or after igni was quit and
started again. Both paths here run real Agno agents (a scripted model,
no network) against a real database — SQLite, and PostgreSQL when
``IGNI_TEST_DATABASE_URL`` names one (``tests/storage_backends.py``):

* the **main agent** pauses on a tool that needs confirmation; the
  backend dies; a new one starts, reads the approval back, the decision
  arrives, and the run is continued by its id and completes;
* a **sub-agent** spawned by the main agent's ``spawn_agent`` pauses; the
  backend dies with the main agent mid-call; a new one starts, the
  decision arrives, the sub-agent is continued, its answer becomes the
  ``spawn_agent`` call's result, and the main agent's run completes
  with it.

"Dies" is what a kill leaves: the objects are abandoned mid-await, no
``finally`` of theirs runs before the new backend starts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run import agent as agent_events
from agno.run.base import RunStatus
from agno.tools import tool

from ember_code.backend.activity import Activity, ActivityState, SessionActivity
from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.pause_handler import PauseHandler
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.core.approvals.journal import ApprovalJournal
from ember_code.core.config.settings import Settings
from ember_code.core.db.database import Database
from ember_code.core.db.engine import cache
from ember_code.core.db.urls import DatabaseUrl
from ember_code.core.memory.scoped_postgres import ScopedAsyncPostgresDb
from ember_code.core.sub_agent_hitl import SubAgentHITLCoordinator
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.orchestrate_budget import SpawnBudget
from ember_code.protocol import messages as msg
from tests.storage_backends import Scopes

SESSION = "cold-s1"
MAIN = "igni"

Script = Callable[[list[Message]], ModelResponse]


class ScriptedModel(Model):
    """A model that answers from a script of the conversation so far."""

    def __init__(self, script: Script) -> None:
        super().__init__(id="scripted", provider="test")
        self._script = script

    def _answer(self, messages: list[Message]) -> ModelResponse:
        response = self._script(messages)
        response.role = "assistant"
        return response

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self._answer(kwargs["messages"])

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._answer(kwargs["messages"])

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def _call(call_id: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    )


def _tool_results(messages: list[Message]) -> list[str]:
    return [str(m.content) for m in messages if m.role == "tool"]


class Side:
    """What the tools did — across both backends."""

    def __init__(self) -> None:
        self.calls: list[str] = []


def _deploy_tool(side: Side) -> Any:
    @tool(requires_confirmation=True)
    def deploy(target: str) -> str:
        """Deploy to a target."""
        side.calls.append(f"deploy {target}")
        return f"deployed {target}"

    return deploy


def _remove_tool(side: Side) -> Any:
    @tool(requires_confirmation=True)
    def remove_build() -> str:
        """Remove the build directory."""
        side.calls.append("remove build")
        return "build removed"

    return remove_build


def _main_deploys(messages: list[Message]) -> ModelResponse:
    results = _tool_results(messages)
    if not results:
        return _call("call-deploy", "deploy", target="prod")
    return ModelResponse(content=f"Done: {results[-1]}")


def _main_spawns(messages: list[Message]) -> ModelResponse:
    results = _tool_results(messages)
    if not results:
        return _call("call-spawn", "spawn_agent", task="clean the build", agent_name="cleaner")
    return ModelResponse(content=f"Main finished after: {results[-1]}")


def _cleaner(messages: list[Message]) -> ModelResponse:
    results = _tool_results(messages)
    if not results:
        return _call("call-remove", "remove_build")
    return ModelResponse(content=f"cleaner says {results[-1]}")


class FakeHooks:
    async def execute(self, **_: Any) -> Any:
        return SimpleNamespace(message="", should_continue=True)


class Collect:
    """The stream a resumed run produces, kept."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, stream: AsyncIterator[Any]) -> AsyncIterator[msg.Message]:
        async for event in stream:
            self.events.append(event)
        return
        yield

    def completed_content(self) -> str:
        done = [e for e in self.events if isinstance(e, agent_events.RunCompletedEvent)]
        return str(done[-1].content) if done else ""


class Store:
    """The databases a backend opens: the same ones after a restart."""

    def __init__(self, url: DatabaseUrl, tmp: Path) -> None:
        self.url = url
        self.tmp = tmp
        self.scope = Scopes.fresh()

    def state(self) -> Database:
        return Database(self.url, scope=self.scope)

    def agno(self) -> Any:
        if self.url.is_sqlite:
            return AsyncSqliteDb(db_file=str(self.tmp / "agno.db"), session_table="ember_sessions")
        return ScopedAsyncPostgresDb(
            scope=self.scope,
            db_engine=cache.get_async_engine(self.url),
            session_table="ember_sessions",
        )


class Backend:
    """One backend's worth of objects on the shared databases."""

    def __init__(
        self,
        store: Store,
        side: Side,
        *,
        main_script: Script,
        with_spawn: bool,
        spawn_timeout: int | None = None,
    ) -> None:
        self.agno_db = store.agno()
        state = store.state()
        journal = ApprovalJournal(state)
        self.store = PendingRequirementsStore(journal, session_id=lambda: SESSION)
        self.store.restore(SESSION)
        self.coordinator = SubAgentHITLCoordinator()
        self.coordinator.attach_journal(journal, lambda: SESSION)
        self.coordinator.restore(SESSION)
        self.pool = FakePool(self.agno_db, side)
        tools: list[Any] = [_deploy_tool(side)]
        settings = Settings()
        if spawn_timeout is not None:
            settings.orchestration.sub_team_timeout = spawn_timeout
        if with_spawn:
            tools = [
                OrchestrateTools(
                    pool=self.pool,
                    settings=settings,
                    session_id=SESSION,
                    hitl_coordinator=self.coordinator,
                )
            ]
        self.main = Agent(
            name=MAIN,
            model=ScriptedModel(main_script),
            tools=tools,
            db=self.agno_db,
            session_id=SESSION,
        )
        self.collect = Collect()
        self.session = SimpleNamespace(
            session_id=SESSION,
            sub_agent_hitl=self.coordinator,
            main_team=self.main,
            pool=self.pool,
            permission_evaluator=None,
            hook_executor=FakeHooks(),
            set_permission_mode=lambda mode: mode,
        )
        self.hitl = HitlController(self.session, self.store, stream_factory=self.collect)

    async def answer(self, requirement_id: str) -> list[msg.Message]:
        decision = msg.HITLDecision(requirement_id=requirement_id, action="confirm")
        return [m async for m in self.hitl.resolve_batch([decision])]


class FakePool:
    """The agent pool, as ``spawn_agent`` reaches it: one specialist."""

    def __init__(self, db: Any, side: Side) -> None:
        self._db = db
        self._side = side
        self._budget = SpawnBudget(max_agents=10)

    def get(self, name: str) -> Agent:
        if name != "cleaner":
            raise KeyError(name)
        return Agent(
            name="cleaner",
            model=ScriptedModel(_cleaner),
            tools=[_remove_tool(self._side)],
            db=self._db,
        )

    def get_definition(self, name: str) -> Any:
        return None

    def spawn_budget(self, session_id: str) -> SpawnBudget:
        return self._budget


async def _stored_run(db: Any, run_id: str) -> Any:
    session = await db.get_session(session_id=SESSION, session_type=SessionType.AGENT)
    return next(r for r in session.runs or [] if r.run_id == run_id)  # type: ignore[union-attr]


async def test_the_main_agents_approval_is_answered_after_a_cold_restart(
    database_url: DatabaseUrl, tmp_path: Path
):
    side = Side()
    store = Store(database_url, tmp_path)
    before = Backend(store, side, main_script=_main_deploys, with_spawn=False)
    paused = None
    async for event in before.main.arun("deploy it", stream=True, stream_events=True):
        if isinstance(event, agent_events.RunPausedEvent):
            paused = event
    assert paused is not None
    [shown] = PauseHandler(evaluator=None, store=before.store).handle(paused).messages
    requirement_id = shown.requirements[0].requirement_id  # type: ignore[attr-defined]
    assert side.calls == []
    del before  # the backend is gone

    after = Backend(store, side, main_script=_main_deploys, with_spawn=False)
    [again] = after.hitl.waiting_messages()
    assert [r.requirement_id for r in again.requirements] == [requirement_id]
    replies = await after.answer(requirement_id)
    assert not [r for r in replies if isinstance(r, msg.Error)], replies

    assert side.calls == ["deploy prod"]
    assert after.collect.completed_content() == "Done: deployed prod"
    run = await _stored_run(after.agno_db, paused.run_id)
    assert run.status == RunStatus.completed
    assert after.hitl.waiting_messages() == []


async def test_a_sub_agents_approval_is_answered_after_a_cold_restart(
    database_url: DatabaseUrl, tmp_path: Path
):
    side = Side()
    store = Store(database_url, tmp_path)
    before = Backend(store, side, main_script=_main_spawns, with_spawn=True)

    async def run_main() -> None:
        async for _ in before.main.arun("tidy up", stream=True, stream_events=True):
            pass

    # The main agent spawns the cleaner, which pauses on its tool: the
    # main agent's run is now waiting inside its ``spawn_agent`` call.
    running = asyncio.create_task(run_main())
    for _ in range(200):
        if before.coordinator.waiting_count:
            break
        await asyncio.sleep(0.01)
    [paused] = before.coordinator.waiting_requests()
    requirement_id = paused.requirements[0].requirement_id
    assert paused.requirements[0].agent_path == ["cleaner"]
    assert side.calls == []

    # The backend dies here: the main run is abandoned mid-call.
    dead = before

    after = Backend(store, side, main_script=_main_spawns, with_spawn=True)
    [again] = after.hitl.waiting_messages()
    assert [r.requirement_id for r in again.requirements] == [requirement_id]
    replies = await after.answer(requirement_id)
    assert not [r for r in replies if isinstance(r, msg.Error)], replies

    assert side.calls == ["remove build"], "the sub-agent's tool ran, once"
    final = after.collect.completed_content()
    assert final.startswith("Main finished after: [Agent: cleaner]")
    assert "cleaner says build removed" in final
    main_run = await _stored_run(after.agno_db, after.collect.events[-1].run_id)
    assert main_run.status == RunStatus.completed
    assert after.hitl.waiting_messages() == []
    assert ApprovalJournal(store.state()).spawn(paused.run_id) is None

    running.cancel()
    del dead


async def test_a_spawn_waits_for_its_approval_as_long_as_the_person_takes(
    database_url: DatabaseUrl, tmp_path: Path
):
    """Approvals have no expiry. The spawn's own timeout is one second;
    the person takes longer, and the spawn still finishes — while it
    waits, the session counts as waiting, not running."""
    side = Side()
    backend = Backend(
        Store(database_url, tmp_path),
        side,
        main_script=_main_spawns,
        with_spawn=True,
        spawn_timeout=1,
    )
    outcome: list[str] = []

    async def run_main() -> None:
        async for event in backend.main.arun("tidy up", stream=True, stream_events=True):
            if isinstance(event, agent_events.RunCompletedEvent):
                outcome.append(str(event.content))

    running = asyncio.create_task(run_main())
    for _ in range(200):
        if backend.coordinator.waiting_count:
            break
        await asyncio.sleep(0.01)
    [paused] = backend.coordinator.waiting_requests()
    tools = backend.main.tools[0]  # type: ignore[index]
    activity = SessionActivity.of(
        SESSION,
        run_in_progress=True,
        waiting_approvals=1,
        blocked_spawns=backend.coordinator.waiting_count,
        active_subagent_runs=len(tools.subagent_registry),
    )
    assert activity.state is ActivityState.WAITING

    await asyncio.sleep(1.5)  # longer than the spawn may work
    assert not running.done()
    backend.coordinator.resolve(paused.requirements[0].requirement_id, "confirm")
    await asyncio.wait_for(running, timeout=10)

    assert side.calls == ["remove build"]
    [final] = outcome
    assert "cleaner says build removed" in final
    assert "timeout" not in final.lower()


def test_a_run_that_only_waits_on_a_person_is_not_running():
    def state(**kwargs: Any) -> ActivityState:
        defaults = {
            "run_in_progress": False,
            "waiting_approvals": 0,
            "blocked_spawns": 0,
            "active_subagent_runs": 0,
        }
        return SessionActivity.of("s", **{**defaults, **kwargs}).state

    assert state() is ActivityState.IDLE
    assert state(waiting_approvals=1) is ActivityState.WAITING
    assert state(run_in_progress=True) is ActivityState.RUNNING
    assert state(run_in_progress=True, blocked_spawns=1, active_subagent_runs=1) is (
        ActivityState.WAITING
    )
    assert state(run_in_progress=True, blocked_spawns=1, active_subagent_runs=2) is (
        ActivityState.RUNNING
    ), "another sub-agent is still working"
    sessions = [
        SessionActivity(session_id="a", state=ActivityState.WAITING, waiting_approvals=1),
        SessionActivity(session_id="b", state=ActivityState.IDLE),
    ]
    assert Activity.of(sessions).suspendable
    sessions.append(SessionActivity(session_id="c", state=ActivityState.RUNNING))
    assert not Activity.of(sessions).suspendable
