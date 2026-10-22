"""``max_nesting_depth`` counts, because nesting now happens.

This file replaces ``test_nesting_depth_is_unreachable.py``, which
asserted the opposite and said what would have to change for it to be
wrong:

    If someone gives spawned agents their own orchestration toolkit,
    this fails and points at the counter that has to start moving with
    it — the alternative being unbounded recursion guarded by a
    setting everybody believes is already working.

That is what happened. ``AgentSpawn._grant_nested_orchestration``
gives an agent declaring ``can_orchestrate`` its own
:class:`OrchestrateTools` built at ``current_depth + 1``, so the guard
in ``SpawnRunner.run`` reads a number that moves.

So the tests here are the ones the old file asked for: exercise the
guard, and hold the two properties that keep the recursion bounded.
The counter moving is the whole safety argument — a grant without an
increment is unbounded recursion behind a setting that reads as if it
were working.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ember_code.core.tools.orchestrate_budget import SpawnBudget
from ember_code.core.tools.orchestrate_events import SpawnDeps, SubAgentRegistry
from ember_code.core.tools.orchestrate_spawn import AgentSpawn


def _settings(max_depth: int = 5, max_agents: int = 20):
    """The real :class:`OrchestrationConfig`, not a namespace.

    ``OrchestrateTools.__init__`` reads more of it than the depth
    guard does — ``generate_ephemeral`` among others — so a double
    built from the fields this file cares about fails on a field it
    does not, for reasons that have nothing to do with nesting.
    """
    from ember_code.core.config.schemas.orchestration import OrchestrationConfig

    return SimpleNamespace(
        orchestration=OrchestrationConfig(
            max_nesting_depth=max_depth,
            max_total_agents=max_agents,
        )
    )


def _spawn(*, depth: int, can_orchestrate: bool, max_depth: int = 5, agent=None):
    """An :class:`AgentSpawn` wired with doubles, at a chosen depth."""
    settings = _settings(max_depth=max_depth)
    defn = SimpleNamespace(
        can_orchestrate=can_orchestrate,
        description="",
        tools=[],
        force_isolation=None,
    )
    agent = agent if agent is not None else SimpleNamespace(tools=[])
    pool = MagicMock()
    pool.get.return_value = agent
    pool.get_definition.return_value = defn
    pool.spawn_budget.return_value = SpawnBudget(settings.orchestration.max_total_agents)

    return AgentSpawn(
        agent_name="archivist",
        task="write it down",
        isolation="none",
        pool=pool,
        settings=settings,
        session_id="s1",
        hook_executor=None,
        on_progress=None,
        event_appender=None,
        subagent_registry=SubAgentRegistry(),
        hitl_coordinator=None,
        budget=pool.spawn_budget.return_value,
        current_depth=depth,
        max_depth=max_depth,
        project_dir=Path("."),
        deps=SpawnDeps(
            agent_streamer=MagicMock(),
            team_streamer=MagicMock(),
            team_factory=MagicMock(),
            model_registry_factory=MagicMock(),
        ),
    )


def _nested(agent):
    """The orchestration toolkit granted to ``agent``, if any."""
    from ember_code.core.tools.orchestrate import OrchestrateTools

    return [t for t in (agent.tools or []) if isinstance(t, OrchestrateTools)]


class TestTheGuardCanFire:
    async def test_a_spawn_at_the_limit_is_refused(self):
        spawn = _spawn(depth=5, can_orchestrate=True, max_depth=5)

        assert await spawn.run() == "Error: Maximum nesting depth (5) reached."

    async def test_the_refusal_names_the_configured_limit(self):
        """Not a constant — somebody lowering it should see their number."""
        spawn = _spawn(depth=2, can_orchestrate=True, max_depth=2)

        assert "(2)" in await spawn.run()


class TestTheCounterMoves:
    def test_a_spawned_agent_is_one_level_deeper_than_its_parent(self):
        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=1, can_orchestrate=True, agent=agent)

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        granted = _nested(agent)
        assert len(granted) == 1
        assert granted[0].current_depth == 2

    def test_the_grant_is_withheld_at_the_last_level(self):
        """Rather than granted and refused on every call.

        A manifest carrying a tool that always errors is the defect
        that cost ``artifact_edit_draft`` its entire workflow.
        """
        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=4, can_orchestrate=True, max_depth=5, agent=agent)

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        assert _nested(agent) == []

    def test_an_agent_that_declines_orchestration_gets_none(self):
        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=0, can_orchestrate=False, agent=agent)

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        assert _nested(agent) == []


class TestTheGrantDoesNotLeak:
    def test_the_pools_cached_agent_is_left_alone(self):
        """``copy.copy`` is shallow, so ``agent.tools`` starts out as
        the very list the pool hands to every future spawn. Appending
        in place would give the next spawn of this agent a toolkit
        built at *this* spawn's depth."""
        shared_list: list = []
        agent = SimpleNamespace(tools=shared_list)
        spawn = _spawn(depth=1, can_orchestrate=True, agent=agent)

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        assert shared_list == [], "the pool's tool list was mutated"
        assert len(_nested(agent)) == 1

    def test_every_depth_draws_on_one_session_wide_budget(self):
        """The cap is per session, not per level — otherwise each new
        level would reset it and the total would be unbounded."""
        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=1, can_orchestrate=True, agent=agent)

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        spawn._pool.spawn_budget.assert_called_with("s1")
        assert _nested(agent)[0]._budget is spawn._budget


class TestAgnoStillReadsToolsAtRunTime:
    """The grant happens after the agent object exists.

    ``AgentBuilder`` constructs the agent with its declared tools;
    the spawn runner adds the orchestration toolkit to the copy just
    before running it. That only works because Agno resolves
    ``entity.tools`` per run rather than freezing a function map at
    construction — ``agno.utils.callables.get_resolved_tools`` does a
    plain ``getattr``.

    An Agno upgrade that moved the resolution into ``__init__`` would
    not fail anything else: the toolkit would sit on the attribute,
    the model would never be offered its functions, and the archivist
    would simply never delegate. Pinned here so that arrives as a
    failing test rather than as a capability that quietly stopped.
    """

    def test_tools_are_resolved_from_the_attribute(self):
        from agno.utils.callables import get_resolved_tools

        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=1, can_orchestrate=True, agent=agent)
        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        resolved = get_resolved_tools(agent)

        assert resolved is agent.tools
        assert _nested(agent)[0] in resolved


class TestTheWiringSurvivesTheHop:
    """A nested spawn that nothing can see or stop is worse than none.

    Progress events draw the card, the appender replays it after a
    reload, and the registry is what ``cancel_run`` iterates — ESC
    does not reach a sub-agent that never registered.
    """

    def test_progress_event_log_and_cancellation_are_carried_down(self):
        agent = SimpleNamespace(tools=[])
        spawn = _spawn(depth=1, can_orchestrate=True, agent=agent)
        progress, appender = MagicMock(), MagicMock()
        spawn._on_progress, spawn._event_appender = progress, appender

        spawn._grant_nested_orchestration(agent, spawn._pool.get_definition())

        granted = _nested(agent)[0]
        assert granted.on_progress is progress
        assert granted.event_appender is appender
        assert granted.subagent_registry is spawn._subagent_registry


class TestTheDeclarationsAreRead:
    """``can_orchestrate`` is a curated field, not a default.

    Twenty bundled agent files set it explicitly, in both directions,
    written when nothing read it. Wiring it up means those files now
    decide behaviour — so this checks the one the artifact work turns
    on, and that the field is still being set deliberately rather than
    drifting to the schema default.
    """

    @staticmethod
    def _frontmatter(name: str) -> dict:
        import yaml

        import ember_code

        path = Path(ember_code.__file__).parent / "bundled_agents" / name
        body = path.read_text(encoding="utf-8")
        return yaml.safe_load(body.split("---")[1])

    def test_the_archivist_may_commission_a_visualization(self):
        assert self._frontmatter("archivist.md")["can_orchestrate"] is True

    def test_the_archivists_manifest_declares_what_it_holds(self):
        """``Orchestrate`` is a reserved declare-don't-resolve name, the
        same as ``Artifacts``: the registry resolves neither, the
        builder attaches both.

        Declaring is not what grants it — ``can_orchestrate`` is. But a
        ``tools:`` list is the manifest a trace gets attributed by, and
        a grant the manifest omits is the drift that had to be repaired
        across 1,370 captured specialist traces.
        """
        declared = self._frontmatter("archivist.md")["tools"]

        assert "Orchestrate" in declared
        assert "Artifacts" in declared

    def test_the_visualizer_is_a_leaf(self):
        """It renders what it is handed. Nothing to delegate to."""
        assert self._frontmatter("visualizer.md")["can_orchestrate"] is False


@pytest.mark.parametrize("name", ["archivist.md", "visualizer.md"])
def test_the_flag_is_stated_rather_than_inherited(name):
    import ember_code

    path = Path(ember_code.__file__).parent / "bundled_agents" / name
    assert "can_orchestrate:" in path.read_text(encoding="utf-8")
