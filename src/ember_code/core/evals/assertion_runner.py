"""Per-check assertion drivers composed by :class:`CaseAssertionRunner`.

Replaces the god-branching ``_apply_case_assertions`` free function.
Each driver:
    * owns its gate (``.should_run(case)``) so the runner doesn't
      re-implement ``if case.expected_tool_calls: …`` for every check;
    * returns a :class:`CheckResult` from ``.run(ctx)``;
    * writes its own subset of :class:`CaseResult` fields via
      ``.apply_to(result, check)``, so no single mutator touches every
      field on the result.

The overall aggregate (``result.passed``) is derived by
:meth:`CaseResult.compute_passed` after every driver has run — killing
the scattered ``all_passed`` bool that used to thread through the
old free-function pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agno.eval.accuracy import AccuracyEval
from agno.eval.reliability import ReliabilityEval
from pydantic import BaseModel, ConfigDict

from ember_code.core.evals.agno_adapter import AgnoResponseAdapter
from ember_code.core.evals.assertions import (
    FileAssertion,
    check_file_assertion,
    check_unexpected_tool_calls,
)
from ember_code.core.evals.loader import EvalCase
from ember_code.core.evals.schemas import (
    CaseResult,
    CheckResult,
    FileCheckResult,
    ToolArgAssertion,
    ToolTraceEntry,
)
from ember_code.core.evals.tool_names import ToolNameCatalog

logger = logging.getLogger(__name__)


class AssertionContext(BaseModel):
    """Snapshot passed to every driver's ``run()`` method.

    Bundling into one model keeps driver signatures uniform and stops
    each new field from cascading through every driver's positional
    args (which is why the old free-function chain had 7+ params).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    case: EvalCase
    adapter: AgnoResponseAdapter
    output_text: str
    tool_trace: list[ToolTraceEntry]
    agent: Any
    judge_model: Any | None
    work_dir: Path | None
    catalog: ToolNameCatalog


class AssertionDriver:
    """Base class for one kind of eval check.

    Subclasses override:
        * :meth:`should_run` — cheap gate based on ``case.*`` fields;
        * :meth:`run` — async, returns a :class:`CheckResult`;
        * :meth:`apply_to` — write the driver's fields on the result.
    """

    async def run(self, ctx: AssertionContext) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def should_run(self, case: EvalCase) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def apply_to(
        self, result: CaseResult, check: CheckResult
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class ReliabilityDriver(AssertionDriver):
    """Runs Agno's ReliabilityEval against ``case.expected_tool_calls``.

    Agno's ReliabilityEval crashes with ``'NoneType' is not reversible``
    when ``response.messages`` is None (typically because the underlying
    LLM run errored out — rate-limited, network failure, etc.). We guard
    that upstream and surface a clean error instead of a stack trace
    pretending to be a tool-call mismatch.

    We also strip errored / unknown tool calls before Agno scans them
    (done in :class:`AgnoResponseAdapter`) — see that class's docstring
    for the full rationale.
    """

    def should_run(self, case: EvalCase) -> bool:
        return bool(case.expected_tool_calls)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        expanded = ctx.catalog.expand_expected(ctx.case.expected_tool_calls or [])
        response = ctx.adapter.response
        if not ctx.adapter.has_messages():
            return CheckResult(
                ok=False,
                detail="agent run produced no response (likely API error / rate limit)",
            )
        try:
            rel = ReliabilityEval(
                agent_response=response,
                expected_tool_calls=expanded,
                print_results=False,
                telemetry=False,
            )
            result = await rel.arun(print_results=False)
            if result is None:
                return CheckResult(ok=False, detail="reliability eval returned None")
            if result.eval_status == "PASSED":
                return CheckResult(ok=True, detail="all expected tools called")
            # When the agent called ZERO tools, Agno reports every expected tool
            # as "failed" (see reliability.py:233 — `failed = expected_tool_calls`).
            # Surface a clearer message — "no tools called" is the actionable signal.
            called_any = bool(getattr(result, "passed_tool_calls", None))
            if (
                not called_any
                and result.failed_tool_calls
                and set(result.failed_tool_calls) == set(expanded)
            ):
                sample = ", ".join(expanded[:4])
                suffix = ", ..." if len(expanded) > 4 else ""
                return CheckResult(
                    ok=False,
                    detail=f"agent called no tools (expected at least one of: {sample}{suffix})",
                )
            failed = ", ".join(result.failed_tool_calls) if result.failed_tool_calls else "unknown"
            return CheckResult(
                ok=False,
                detail=f"unexpected tool calls (outside allowlist): {failed}",
            )
        except Exception as exc:
            return CheckResult(ok=False, detail=f"reliability eval error: {exc}")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.reliability_passed = check.ok
        result.reliability_detail = check.detail


class UnexpectedToolsDriver(AssertionDriver):
    """Blocklist check — fails if any forbidden tool was called."""

    def should_run(self, case: EvalCase) -> bool:
        return bool(case.unexpected_tool_calls)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        expanded = ctx.catalog.expand(ctx.case.unexpected_tool_calls or [])
        passed, detail = check_unexpected_tool_calls(ctx.adapter.response, expanded)
        return CheckResult(ok=passed, detail=detail)

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.unexpected_passed = check.ok
        result.unexpected_detail = check.detail


class AccuracyDriver(AssertionDriver):
    """Runs Agno's AccuracyEval against ``case.expected_output``.

    Skipped when no judge model is available or the case doesn't
    specify an expected output.
    """

    def should_run(self, case: EvalCase) -> bool:
        return bool(case.expected_output)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        if ctx.judge_model is None:
            # Gate is defined by (expected_output AND judge_model);
            # the "no judge configured" path is treated as skipped.
            return CheckResult(ok=True, detail="no judge model configured, skipped")
        try:
            acc = AccuracyEval(
                agent=ctx.agent,
                input=ctx.case.input,
                expected_output=ctx.case.expected_output,
                model=ctx.judge_model,
                additional_guidelines=ctx.case.judge_guidelines,
                num_iterations=ctx.case.num_iterations,
                print_summary=False,
                print_results=False,
                telemetry=False,
            )
            result = await acc.arun_with_output(
                output=ctx.output_text,
                print_summary=False,
                print_results=False,
            )
            if result is None:
                return CheckResult(ok=False, detail="accuracy eval returned None")
            score = result.avg_score
            threshold = ctx.case.accuracy_threshold
            passed = score >= threshold
            # Concat per-iteration reasons. Usually 1 iter, but support all.
            reasons: list[str] = []
            for r in getattr(result, "results", None) or []:
                reason = getattr(r, "reason", None)
                if reason:
                    reasons.append(reason)
            reason_text = "\n---\n".join(reasons)
            return CheckResult(
                ok=passed,
                detail=f"score {score:.1f}/{threshold}",
                score=score,
                reason=reason_text,
            )
        except Exception as exc:
            return CheckResult(ok=False, detail=f"accuracy eval error: {exc}")

    def should_run_with_judge(self, case: EvalCase, judge_model: Any | None) -> bool:
        """Extended gate — the runner uses this in place of :meth:`should_run`
        because the judge availability is a *context* concern, not a case one.
        """
        return bool(case.expected_output) and judge_model is not None

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        # Only overwrite when we actually ran (score present or a real
        # ok/fail signal came back). We deliberately keep accuracy_score
        # None for the "no judge configured" skipped path.
        if check.score is not None or check.detail.startswith("accuracy eval"):
            result.accuracy_passed = check.ok
            result.accuracy_score = check.score
            result.accuracy_reason = check.reason


class ToolArgDriver(AssertionDriver):
    """Verifies each :class:`ToolArgAssertion` matched at least one call.

    An assertion passes when *some* call to ``tool`` has every key/value
    in ``args_must_contain`` present in its captured ``args`` dict. Used
    for "did spawn_team get called with mode='coordinate'?"-style checks.

    Can also be used stand-alone via :meth:`check` (used by tests to
    verify assertion behaviour without spinning up a full runner).
    """

    def should_run(self, case: EvalCase) -> bool:
        return bool(case.tool_arg_assertions)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        assertions = ToolArgDriver._coerce_assertions(ctx.case.tool_arg_assertions or [])
        return self.check(ctx.tool_trace, assertions)

    @staticmethod
    def _coerce_assertions(
        raw: list[dict[str, Any]] | list[ToolArgAssertion],
    ) -> list[ToolArgAssertion]:
        out: list[ToolArgAssertion] = []
        for a in raw:
            if isinstance(a, ToolArgAssertion):
                out.append(a)
            elif isinstance(a, dict):
                out.append(ToolArgAssertion.model_validate(a))
        return out

    @staticmethod
    def check(
        tool_trace: list[ToolTraceEntry],
        assertions: list[ToolArgAssertion] | list[dict[str, Any]],
    ) -> CheckResult:
        """Public entry point used by :class:`CaseAssertionRunner` and tests.

        Accepts either validated :class:`ToolArgAssertion` instances or
        raw dicts (legacy shape). Malformed entries missing ``tool`` are
        silently skipped — matches the pre-refactor behaviour where such
        rows never matched anything.
        """
        typed = ToolArgDriver._coerce_assertions(assertions)
        failures: list[str] = []
        for a in typed:
            target_tool = a.tool
            required = a.args_must_contain or {}
            if not target_tool:
                continue
            matched = False
            for call in tool_trace:
                if call.name != target_tool:
                    continue
                args = call.args or {}
                if all(args.get(k) == v for k, v in required.items()):
                    matched = True
                    break
            if not matched:
                failures.append(f"{target_tool}({required})")
        if failures:
            return CheckResult(
                ok=False,
                detail="missing tool-arg matches: " + ", ".join(failures),
            )
        return CheckResult(ok=True, detail="all tool-arg assertions matched")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.tool_arg_passed = check.ok
        result.tool_arg_detail = check.detail


class FileDriver(AssertionDriver):
    """Walks ``case.file_assertions`` and emits one
    :class:`FileCheckResult` per assertion.

    Aggregates into ``result.file_results`` via
    :meth:`CaseResult.add_file_result`. The overall pass/fail for
    "did every file assertion pass" is computed in
    :meth:`CaseResult.compute_passed` — this driver only writes the
    per-assertion outcomes.
    """

    def should_run(self, case: EvalCase) -> bool:
        return bool(case.file_assertions)

    async def run(self, ctx: AssertionContext) -> CheckResult:  # pragma: no cover - special case
        # File assertions produce a list of per-file results, not a single
        # CheckResult. The runner calls :meth:`run_all` directly instead
        # of the base ``run`` — this ``run`` implementation exists only
        # to satisfy the abstract contract if anyone calls it uniformly.
        results = self.run_all(ctx)
        every_ok = all(r.passed for r in results)
        detail = ""
        if not every_ok:
            first_fail = next((r for r in results if not r.passed), None)
            if first_fail is not None:
                detail = f"{first_fail.type}: {first_fail.detail}"
        return CheckResult(ok=every_ok, detail=detail)

    def run_all(self, ctx: AssertionContext) -> list[FileCheckResult]:
        results: list[FileCheckResult] = []
        for assertion in ctx.case.file_assertions or []:
            passed, detail = check_file_assertion(assertion, work_dir=ctx.work_dir)
            atype = FileDriver._extract_type(assertion)
            results.append(FileCheckResult(type=atype, passed=passed, detail=detail))
        return results

    @staticmethod
    def _extract_type(assertion: Any) -> str:
        if isinstance(assertion, FileAssertion):
            return assertion.type
        if isinstance(assertion, dict):
            return assertion.get("type", "")
        return getattr(assertion, "type", "")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:  # pragma: no cover
        # No-op; file results are written directly via
        # :meth:`CaseResult.add_file_result` during :meth:`CaseAssertionRunner.run`.
        return None


class CaseAssertionRunner:
    """Composes every :class:`AssertionDriver` and applies them to a case.

    Iteration order is intentional (reliability → unexpected → accuracy
    → tool-arg → file) so the human-readable per-check report keeps its
    historical column order.
    """

    def __init__(self, drivers: list[AssertionDriver] | None = None) -> None:
        self._reliability = ReliabilityDriver()
        self._unexpected = UnexpectedToolsDriver()
        self._accuracy = AccuracyDriver()
        self._tool_arg = ToolArgDriver()
        self._file = FileDriver()
        self._drivers: list[AssertionDriver] = drivers or [
            self._reliability,
            self._unexpected,
            self._accuracy,
            self._tool_arg,
        ]

    async def run(self, ctx: AssertionContext, result: CaseResult) -> None:
        """Populate ``result`` with per-check outcomes from every driver."""
        for driver in self._drivers:
            if isinstance(driver, AccuracyDriver):
                if not driver.should_run_with_judge(ctx.case, ctx.judge_model):
                    continue
            elif not driver.should_run(ctx.case):
                continue
            check = await driver.run(ctx)
            driver.apply_to(result, check)

        # File assertions produce many per-file results, not one CheckResult
        # — handle them separately so we don't lose per-file granularity.
        if self._file.should_run(ctx.case):
            for fr in self._file.run_all(ctx):
                result.add_file_result(fr)

        result.compute_passed()
