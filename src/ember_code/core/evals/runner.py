"""Eval runner — orchestrates agent runs and Agno eval checks.

The module used to be one 720-LoC bag of free functions with a
state-first-arg calling convention. The behaviour now lives on two
classes:

    * :class:`CaseRunner` — one instance per eval case; drives one
      ``agent.arun()`` cycle plus every assertion driver.
    * :class:`SuiteRunner` — one instance per suite; owns the
      per-suite workspace, judge model, and agent lookup, then
      iterates cases.

All Pydantic wire schemas moved to :mod:`.schemas`. All tool-name
expansion moved to :mod:`.tool_names`. Workspace / fixture / setup
lifecycle moved to :mod:`.workspace`. Agno response mangling moved to
:mod:`.agno_adapter`. Per-check driver logic moved to
:mod:`.assertion_runner`.

The module re-exports :class:`CaseResult`, :class:`SuiteResult`, and
:class:`ToolTraceEntry` from :mod:`.schemas` so existing imports
(``from ember_code.core.evals.runner import CaseResult``) keep
working without touching call sites.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.agent import Agent

from ember_code.core.config.models import ModelRegistry
from ember_code.core.evals.agno_adapter import AgnoResponseAdapter
from ember_code.core.evals.assertion_runner import (
    AssertionContext,
    CaseAssertionRunner,
)
from ember_code.core.evals.loader import EvalSuite
from ember_code.core.evals.schemas import (
    AgnoRunOptions,
    CaseResult,
    SuiteResult,
    ToolTraceEntry,
)
from ember_code.core.evals.tool_names import DEFAULT_CATALOG, ToolNameCatalog
from ember_code.core.evals.workspace import EvalWorkspace

if TYPE_CHECKING:
    from ember_code.core.agents import AgentPool
    from ember_code.core.config.settings import Settings
    from ember_code.core.evals.loader import EvalCase

logger = logging.getLogger(__name__)


__all__ = [
    "CaseResult",
    "SuiteResult",
    "ToolTraceEntry",
    "CaseRunner",
    "SuiteRunner",
]


class CaseRunner:
    """Runs one eval case against an already-built agent.

    ``session_id`` should be unique per case — Agno agents load prior
    history from the session DB on every ``arun()`` call, so reusing
    the agent's default session_id across cases pollutes
    ``response.messages`` with tool calls from earlier cases. The
    reliability check then sees a mishmash of tools and reports false
    failures. Pass a fresh UUID per case to isolate.
    """

    def __init__(
        self,
        case: EvalCase,
        agent: Agent,
        judge_model: Any | None = None,
        session_id: str | None = None,
        work_dir: Path | None = None,
        catalog: ToolNameCatalog | None = None,
        assertion_runner: CaseAssertionRunner | None = None,
    ) -> None:
        self._case = case
        self._agent = agent
        self._judge_model = judge_model
        self._session_id = session_id
        self._work_dir = work_dir
        self._catalog = catalog or DEFAULT_CATALOG
        self._assertion_runner = assertion_runner or CaseAssertionRunner()

    async def run(self) -> CaseResult:
        """Execute the case end-to-end and return the populated result."""
        result = CaseResult(case=self._case)
        start = time.monotonic()

        try:
            try:
                adapter = await self._execute_arun()
            except Exception as run_exc:
                result.error = f"agent.arun failed: {run_exc}"
                result.elapsed = time.monotonic() - start
                return result

            result.response_text = adapter.output_text
            result.tool_trace = adapter.tool_trace()

            ctx = AssertionContext(
                case=self._case,
                adapter=adapter,
                output_text=adapter.output_text,
                tool_trace=result.tool_trace,
                agent=self._agent,
                judge_model=self._judge_model,
                work_dir=self._work_dir,
                catalog=self._catalog,
            )
            await self._assertion_runner.run(ctx, result)

        except Exception as exc:
            result.error = str(exc)
            result.passed = False

        result.elapsed = time.monotonic() - start
        return result

    async def _execute_arun(self) -> AgnoResponseAdapter:
        """Fire the agent's ``arun`` for the case then normalise the response.

        Multi-turn cases send each prior message first on the same
        session, then send the actual input. We only judge the response
        to the last turn — prior turns are setup. We don't sleep
        between turns; the model produces a complete response per arun,
        and Agno persists it before returning.
        """
        options = AgnoRunOptions(stream=False, session_id=self._session_id)
        run_kwargs = options.dump()
        for prior in self._case.prior_messages:
            await self._agent.arun(prior, **run_kwargs)
        response = await self._agent.arun(self._case.input, **run_kwargs)

        # Strip ``from_history=True`` messages so every downstream
        # check sees only the final turn's tool calls. Prior_messages
        # are sent on the same session_id and Agno reloads them into
        # ``response.messages`` with ``from_history=True``. Without
        # this filter, multi-turn cases false-fail on tools the agent
        # called in turn 1.
        #
        # Also strip errored tool calls — Agno keeps calls that raised
        # in ``response.messages`` (so the agent can retry with the
        # error text). Reliability checks scan tool names verbatim and
        # would false-fail on those hallucinated calls.
        adapter = AgnoResponseAdapter(response)
        adapter.strip_from_history()
        adapter.strip_errored_tool_calls()
        return adapter


class SuiteRunner:
    """Runs every case in one eval suite.

    Owns the per-suite work_dir (via :class:`EvalWorkspace`), the
    judge model lookup, and the agent lookup from the pool. Each case
    gets its own :class:`CaseRunner` instance.
    """

    def __init__(
        self,
        suite: EvalSuite,
        pool: AgentPool,
        settings: Settings,
        project_dir: Path,
        catalog: ToolNameCatalog | None = None,
    ) -> None:
        self._suite = suite
        self._pool = pool
        self._settings = settings
        self._project_dir = project_dir
        self._catalog = catalog or DEFAULT_CATALOG

    async def run(self) -> SuiteResult:
        """Run every case in the suite; return the aggregated result."""
        suite_result = SuiteResult(suite=self._suite)

        agent = self._lookup_agent(suite_result)
        if agent is None:
            return suite_result

        fixtures_root = self._resolve_fixtures_root()
        judge_model = self._lookup_judge_model()

        workspace = EvalWorkspace(
            fixtures=self._suite.fixtures,
            fixtures_root=fixtures_root,
            setup_module=self._suite.setup_module,
            project_dir=self._project_dir,
        )
        # Split setup and body: setup failure means no case can run
        # (record one error per case); body failures are reported
        # per-case by CaseRunner and don't bubble up here.
        try:
            work_dir = await workspace.setup()
        except Exception as exc:
            logger.exception("setup_module %s failed", self._suite.setup_module)
            for case in self._suite.cases:
                suite_result.case_results.append(
                    CaseResult(case=case, error=f"setup_module failed: {exc}")
                )
            return suite_result
        try:
            await self._run_cases(agent, judge_model, work_dir, suite_result)
        finally:
            await workspace.teardown()
        return suite_result

    async def _run_cases(
        self,
        agent: Agent,
        judge_model: Any | None,
        work_dir: Path,
        suite_result: SuiteResult,
    ) -> None:
        """Iterate cases, printing per-case progress for background runs.

        Per-case progress prints help when the runner is launched as a
        background process and stdout is otherwise silent for minutes
        at a time.
        """
        total = len(self._suite.cases)
        for idx, case in enumerate(self._suite.cases, start=1):
            print(f"  [case {idx}/{total}] {case.name} starting...", flush=True)
            t0 = time.monotonic()
            case_runner = CaseRunner(
                case=case,
                agent=agent,
                judge_model=judge_model,
                work_dir=work_dir,
                catalog=self._catalog,
            )
            case_result = await case_runner.run()
            elapsed = time.monotonic() - t0
            verdict = "PASS" if case_result.passed else "FAIL"
            print(
                f"  [case {idx}/{total}] {case.name} → {verdict} in {elapsed:.1f}s",
                flush=True,
            )
            suite_result.case_results.append(case_result)

    def _lookup_agent(self, suite_result: SuiteResult) -> Agent | None:
        """Resolve the suite's agent from the pool.

        On failure, appends an error result for every case in the suite
        and returns ``None`` — the caller treats that as "suite done".
        """
        try:
            return self._pool.get(self._suite.agent)
        except (KeyError, ValueError) as exc:
            for case in self._suite.cases:
                suite_result.case_results.append(
                    CaseResult(
                        case=case,
                        error=f"agent '{self._suite.agent}' not found: {exc}",
                    )
                )
            return None

    def _resolve_fixtures_root(self) -> Path:
        """Look in ``evals/fixtures/`` first (committed datasets shipped
        with the repo), fall back to ``.ember/evals/`` for user-authored
        fixtures.
        """
        builtin = self._project_dir / "evals" / "fixtures"
        user = self._project_dir / ".ember" / "evals"
        return builtin if builtin.is_dir() else user

    def _lookup_judge_model(self) -> Any | None:
        """Load the configured judge model, or ``None`` if unavailable."""
        try:
            registry = ModelRegistry(self._settings)
            judge_cfg = getattr(self._settings, "evals", None)
            judge_name = getattr(judge_cfg, "judge_model", None) if judge_cfg else None
            return registry.get_model(judge_name)
        except Exception as exc:
            logger.debug("Could not load judge model: %s", exc)
            return None

    @classmethod
    async def run_all(
        cls,
        pool: AgentPool,
        settings: Settings,
        project_dir: Path,
        agent_filter: str | None = None,
        catalog: ToolNameCatalog | None = None,
    ) -> list[SuiteResult]:
        """Load and run every discovered suite; optionally filter by agent name."""
        suites = EvalSuite.load_all(project_dir)
        if not suites:
            return []

        if agent_filter:
            suites = [s for s in suites if s.agent == agent_filter]

        results: list[SuiteResult] = []
        for suite in suites:
            runner = cls(suite, pool, settings, project_dir, catalog=catalog)
            results.append(await runner.run())
        return results
