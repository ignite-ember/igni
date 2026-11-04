"""Evals: config-driven defaults (SWU-037), per-case timeout (SWU-038),
and the WebFetch tool-name mapping (SWU-039).

Each test here pins behaviour that was previously absent:

* ``EvalsConfig.num_iterations`` / ``accuracy_threshold`` were declared
  and documented but never read — the loader hardcoded 1 / 7.0.
* ``EvalCase.case_timeout`` was honoured only by ``scripts/``; the
  in-product ``CaseRunner`` ignored it, so a hung case hung the suite.
* ``ToolNameCatalog`` expanded ``WebFetch`` to the *toolkit* name
  ``ember_web`` (never a tool_call) and omitted ``fetch_json``.
"""

from __future__ import annotations

import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.config.schemas.llm_behavior import EvalsConfig
from ember_code.core.evals.loader import EvalCase, EvalSuite, load_eval_file
from ember_code.core.evals.runner import CaseRunner, SuiteRunner
from ember_code.core.evals.tool_names import DEFAULT_CATALOG
from ember_code.core.paths import CONFIG_DIR

BARE_CASE_YAML = textwrap.dedent("""\
    agent: explorer
    cases:
      - name: bare
        input: "search for something"
      - name: pinned
        input: "search harder"
        num_iterations: 9
        accuracy_threshold: 2.5
""")


# ── SWU-037: EvalsConfig fields are actually read ──────────────────


class TestLoaderHonoursEvalsConfig:
    def test_schema_defaults_apply_when_no_config_passed(self, tmp_path):
        f = tmp_path / "explorer.yaml"
        f.write_text(BARE_CASE_YAML)

        case = load_eval_file(f).cases[0]
        # Was hardcoded to 1 in the loader while EvalsConfig declared 3.
        assert case.num_iterations == EvalsConfig().num_iterations == 3
        assert case.accuracy_threshold == EvalsConfig().accuracy_threshold == 7.0

    def test_config_overrides_the_defaults(self, tmp_path):
        f = tmp_path / "explorer.yaml"
        f.write_text(BARE_CASE_YAML)

        defaults = EvalsConfig(num_iterations=5, accuracy_threshold=8.5)
        case = load_eval_file(f, defaults).cases[0]
        assert case.num_iterations == 5
        assert case.accuracy_threshold == 8.5

    def test_per_case_yaml_still_wins_over_config(self, tmp_path):
        f = tmp_path / "explorer.yaml"
        f.write_text(BARE_CASE_YAML)

        defaults = EvalsConfig(num_iterations=5, accuracy_threshold=8.5)
        case = load_eval_file(f, defaults).cases[1]
        assert case.num_iterations == 9
        assert case.accuracy_threshold == 2.5

    def test_load_all_threads_defaults_through(self, tmp_path):
        evals_dir = tmp_path / CONFIG_DIR / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "explorer.yaml").write_text(BARE_CASE_YAML)

        suites = EvalSuite.load_all(tmp_path, EvalsConfig(num_iterations=4))
        assert suites[0].cases[0].num_iterations == 4

    def test_evals_config_lookup_tolerates_stub_settings(self):
        # MagicMock settings (used all over the test suite) return a
        # MagicMock for ``.evals``; that must not leak into the runner.
        assert SuiteRunner.evals_config(MagicMock()) == EvalsConfig()
        assert SuiteRunner.evals_config(None) == EvalsConfig()

        real = EvalsConfig(timeout_per_case=11)
        settings = MagicMock()
        settings.evals = real
        assert SuiteRunner.evals_config(settings) is real


# ── SWU-038: CaseRunner honours case_timeout ───────────────────────


def _hanging_agent(delay: float = 30.0) -> MagicMock:
    """An agent whose ``arun`` never finishes within the test budget."""

    async def _never(*_args, **_kwargs):
        await asyncio.sleep(delay)
        raise AssertionError("slow case should have been cut short")

    agent = MagicMock()
    agent.arun = AsyncMock(side_effect=_never)
    return agent


def _fast_agent() -> MagicMock:
    agent = MagicMock()
    response = MagicMock()
    response.content = "Done"
    response.tools = None
    response.messages = []
    agent.arun = AsyncMock(return_value=response)
    return agent


class TestCaseTimeout:
    @pytest.mark.asyncio
    async def test_slow_case_fails_with_case_timeout_instead_of_raising(self):
        case = EvalCase(name="slow", input="hang", case_timeout=0.1)

        result = await CaseRunner(case, _hanging_agent(), judge_model=None).run()

        assert result.passed is False
        assert result.error is not None
        assert result.error.startswith("case_timeout: exceeded")

    @pytest.mark.asyncio
    async def test_timeout_does_not_kill_the_rest_of_the_suite(self):
        # The whole point: a hung case yields a *result*, so a caller
        # iterating cases keeps going.
        slow = EvalCase(name="slow", input="hang", case_timeout=0.1)
        fast = EvalCase(name="fast", input="ok")

        results = [
            await CaseRunner(slow, _hanging_agent(), judge_model=None).run(),
            await CaseRunner(fast, _fast_agent(), judge_model=None).run(),
        ]
        assert [r.passed for r in results] == [False, True]

    @pytest.mark.asyncio
    async def test_config_timeout_applies_when_case_has_none(self):
        case = EvalCase(name="slow", input="hang")  # no case_timeout

        runner = CaseRunner(case, _hanging_agent(), judge_model=None, default_timeout=0.1)
        result = await runner.run()

        assert result.passed is False
        assert result.error.startswith("case_timeout: exceeded")

    @pytest.mark.asyncio
    async def test_a_normal_case_is_unaffected_by_a_generous_timeout(self):
        case = EvalCase(name="ok", input="x", case_timeout=30)

        result = await CaseRunner(case, _fast_agent(), judge_model=None).run()

        assert result.passed is True
        assert result.error is None

    def test_effective_timeout_resolution(self):
        agent = _fast_agent()

        def timeout_of(case_timeout, default):
            case = EvalCase(name="t", input="x", case_timeout=case_timeout)
            return CaseRunner(case, agent, default_timeout=default).timeout

        assert timeout_of(5, 600) == 5.0  # per-case wins
        assert timeout_of(None, 600) == 600.0  # config fallback
        assert timeout_of(None, None) is None  # unbounded
        assert timeout_of(None, 0) is None  # 0 disables


# ── SWU-039: WebFetch maps to the real tool functions ──────────────


class TestWebFetchExpansion:
    def test_expands_to_both_registered_functions(self):
        assert DEFAULT_CATALOG.expand(["WebFetch"]) == ["fetch_url", "fetch_json"]

    def test_toolkit_name_is_not_a_tool_name(self):
        # ``ember_web`` is what WebTools passes as ``name=`` to Agno; it
        # can never appear in ``response.tools``.
        assert "ember_web" not in DEFAULT_CATALOG.expand(["WebFetch"])

    @pytest.mark.parametrize("called", ["fetch_url", "fetch_json"])
    @pytest.mark.asyncio
    async def test_anti_case_blocklist_catches_either_web_call(self, called):
        """Mirrors the committed anti-cases (6 in evals/web_discipline.yaml
        + 1 in evals/multiturn.yaml) that list ``WebFetch`` under
        ``unexpected_tool_calls``. Before the mapping fix, ``fetch_json``
        sailed through all of them.
        """
        case = EvalCase(
            name="web_no_fetch_for_generic_concept",
            input="What's the difference between TCP and UDP?",
            unexpected_tool_calls=["WebSearch", "WebFetch"],
        )

        agent = MagicMock()
        response = MagicMock()
        response.content = "TCP is connection-oriented..."
        tool = MagicMock()
        tool.tool_name = called
        response.tools = [tool]
        agent.arun = AsyncMock(return_value=response)

        result = await CaseRunner(case, agent, judge_model=None).run()

        assert result.unexpected_passed is False
        assert called in result.unexpected_detail
        assert result.passed is False
