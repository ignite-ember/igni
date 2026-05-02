"""Eval runner — orchestrates agent runs and Agno eval checks."""

import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ember_code.core.evals.assertions import check_file_assertion, check_unexpected_tool_calls
from ember_code.core.evals.loader import EvalCase, EvalSuite

logger = logging.getLogger(__name__)

# Maps registry display names (used in YAML for readability) to the real
# Agno function names that appear in tool_calls. Each display name expands
# to ALL of its toolkit's functions — listing "Read" in expected_tool_calls
# means "read_file OR read_file_chunk OR list_files are all permitted."
# Unknown names (e.g. spawn_agent / spawn_team) pass through unchanged so
# orchestration tool names work without translation.
_TOOL_NAME_EXPANSIONS: dict[str, list[str]] = {
    "Read": ["read_file", "read_file_chunk", "list_files"],
    "Write": ["save_file", "create_file"],
    "Edit": ["edit_file", "edit_file_replace_all"],
    "Bash": [
        "run_shell_command",
        "read_process_output",
        "watch_process",
        "stop_process",
        "list_processes",
    ],
    "Grep": ["grep", "grep_files", "grep_count"],
    "Glob": ["glob_files"],
    "LS": ["list_files"],
    "WebSearch": ["web_search", "search_news"],
    "WebFetch": ["ember_web"],
    # All ScheduleTools functions — without this, ``Schedule`` falls
    # through unchanged and the matcher looks for a literal function
    # name "Schedule" that never exists, so positive cases that
    # *correctly* call ``schedule_task`` fail with "missing tool
    # calls: schedule_task" and anti-cases pass for the wrong reason
    # (no function literally named "Schedule" was ever going to
    # match, so the "unexpected" check was always trivially clean).
    "Schedule": ["schedule_task", "list_scheduled_tasks", "cancel_scheduled_task"],
}


def _expand_tool_names(names: list[str]) -> list[str]:
    """Expand display names → function names; pass unknown names through."""
    out: list[str] = []
    for n in names:
        for fn in _TOOL_NAME_EXPANSIONS.get(n, [n]):
            if fn not in out:
                out.append(fn)
    return out


class CaseResult(BaseModel):
    """Result of running a single eval case.

    Captures enough trace data to actually iterate prompts after a
    failure: the agent's full response, every tool call (name + args +
    truncated result), and the LLM judge's reasoning.
    """

    case: EvalCase
    passed: bool = False

    # ── Agent run trace (captured even on pass — useful for analysis) ─
    response_text: str = ""
    tool_trace: list[dict] = Field(default_factory=list)
    """Each entry: {name, args, result_preview, error}. Order matches call order."""

    # ── Per-check results ────────────────────────────────────────────
    reliability_passed: bool | None = None
    reliability_detail: str = ""
    unexpected_passed: bool | None = None
    unexpected_detail: str = ""
    accuracy_score: float | None = None
    accuracy_passed: bool | None = None
    accuracy_reason: str = ""
    """The LLM judge's free-text reasoning. Empty when no judge ran."""
    file_results: list[tuple[str, bool, str]] = Field(default_factory=list)

    error: str | None = None
    elapsed: float = 0.0


class SuiteResult(BaseModel):
    """Result of running all cases in an eval suite."""

    suite: EvalSuite
    case_results: list[CaseResult] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.case_results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.case_results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.case_results)

    @property
    def elapsed(self) -> float:
        return sum(r.elapsed for r in self.case_results)

    @classmethod
    async def run(
        cls,
        suite: EvalSuite,
        pool: Any,
        settings: Any,
        project_dir: Path,
    ) -> "SuiteResult":
        """Run all cases in an eval suite."""
        suite_result = cls(suite=suite)

        # Get the agent
        try:
            agent = pool.get(suite.agent)
        except (KeyError, ValueError) as exc:
            for case in suite.cases:
                suite_result.case_results.append(
                    CaseResult(case=case, error=f"agent '{suite.agent}' not found: {exc}")
                )
            return suite_result

        # Set up fixtures. Look in evals/fixtures/ first (committed
        # datasets shipped with the repo), fall back to .ember/evals/ for
        # user-authored fixtures.
        builtin_fixtures = project_dir / "evals" / "fixtures"
        user_fixtures = project_dir / ".ember" / "evals"
        fixtures_root = builtin_fixtures if builtin_fixtures.is_dir() else user_fixtures
        work_dir = _setup_fixtures(suite.fixtures, fixtures_root)

        # Get judge model for accuracy evals
        judge_model = None
        try:
            from ember_code.core.config.models import ModelRegistry

            registry = ModelRegistry(settings)
            judge_name = getattr(settings, "evals", None)
            judge_name = getattr(judge_name, "judge_model", None) if judge_name else None
            judge_model = registry.get_model(judge_name)
        except Exception as exc:
            logger.debug("Could not load judge model: %s", exc)

        # Run each case
        for case in suite.cases:
            case_result = await run_eval_case(case, agent, judge_model)
            suite_result.case_results.append(case_result)

        _cleanup_work_dir(work_dir)
        return suite_result

    @classmethod
    async def run_all(
        cls,
        pool: Any,
        settings: Any,
        project_dir: Path,
        agent_filter: str | None = None,
    ) -> list["SuiteResult"]:
        """Load and run all eval suites, optionally filtered by agent name."""
        suites = EvalSuite.load_all(project_dir)
        if not suites:
            return []

        if agent_filter:
            suites = [s for s in suites if s.agent == agent_filter]

        results = []
        for suite in suites:
            result = await cls.run(suite, pool, settings, project_dir)
            results.append(result)
        return results


def _setup_fixtures(
    fixtures: list[dict] | None,
    fixtures_root: Path,
    work_dir: Path | None = None,
) -> Path:
    """Set up a temp directory with fixtures. Returns the work directory.

    ``target`` paths are interpreted relative to ``work_dir`` (the temp
    directory). ``source`` is resolved relative to ``fixtures_root``
    (e.g. ``evals/fixtures/`` for committed datasets, or
    ``.ember/evals/`` for user-authored ones).
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="ember-eval-"))
    if not fixtures:
        return work_dir

    for fix in fixtures:
        src = fixtures_root / fix.get("source", "")
        target_rel = Path(fix.get("target", ""))
        if not src.exists() or not target_rel.parts:
            logger.debug("fixture skip: src=%s exists=%s target=%s", src, src.exists(), target_rel)
            continue
        target = work_dir / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)
    return work_dir


def _cleanup_work_dir(work_dir: Path) -> None:
    """Remove the temporary work directory."""
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        logger.debug("Failed to clean up eval work dir %s: %s", work_dir, exc)


async def _run_reliability(
    response: Any,
    expected: list[str],
) -> tuple[bool, str]:
    """Run Agno ReliabilityEval on the response.

    Agno's ReliabilityEval crashes with ``'NoneType' is not reversible``
    when ``response.messages`` is None (typically because the underlying
    LLM run errored out — rate-limited, network failure, etc.). Guard
    that case here so an API failure surfaces as a clean error string,
    not a stack trace pretending to be a tool-call mismatch.

    Also strips errored / unknown tool calls from messages before
    Agno scans them. When the model hallucinates a tool name like
    ``"Read"`` and Agno rejects it ("Function Read not found"), the
    rejected call still lands in the message log. Counting these as
    real tool calls produces false reliability failures — the agent
    didn't actually use them, the runtime refused.
    """
    if response is None or getattr(response, "messages", None) is None:
        return False, "agent run produced no response (likely API error / rate limit)"
    try:
        from agno.eval.reliability import ReliabilityEval

        rel = ReliabilityEval(
            agent_response=response,
            expected_tool_calls=expected,
            print_results=False,
            telemetry=False,
        )
        result = await rel.arun(print_results=False)
        if result is None:
            return False, "reliability eval returned None"
        if result.eval_status == "PASSED":
            return True, "all expected tools called"
        # When the agent called ZERO tools, Agno reports every expected tool
        # as "failed" (see reliability.py:233 — `failed = expected_tool_calls`).
        # Surface a clearer message — "no tools called" is the actionable signal.
        called_any = bool(getattr(result, "passed_tool_calls", None))
        if (
            not called_any
            and result.failed_tool_calls
            and set(result.failed_tool_calls) == set(expected)
        ):
            return False, "agent called no tools (expected at least one of: " + ", ".join(
                expected[:4]
            ) + (", ..." if len(expected) > 4 else "") + ")"
        failed = ", ".join(result.failed_tool_calls) if result.failed_tool_calls else "unknown"
        return False, f"missing tool calls: {failed}"
    except Exception as exc:
        return False, f"reliability eval error: {exc}"


def _strip_errored_tool_calls(messages: list) -> list:
    """Return a copy of ``messages`` with errored tool calls scrubbed.

    A tool call is considered errored when:
      - Its corresponding ``role='tool'`` reply has ``tool_call_error``
        set, OR the content starts with ``"Function ... not found"`` /
        ``"ValidationError"`` / similar runtime rejection markers.

    We can't see the tool reply directly from the call, so we walk
    messages in order and track which tool_call_ids had error replies,
    then strip those from the assistant's tool_calls list.
    """
    errored_ids: set[str] = set()
    # Specific markers ONLY — patterns that uniquely identify a runtime
    # rejection of an unknown/invalid tool call. Generic strings like
    # "not found" are dangerous: tool results often contain "no matches
    # found", "file not found", etc. as legitimate output. Stripping
    # those would erase real tool calls and the reliability check would
    # report every call as missing.
    error_re_patterns = (
        "Function ",  # Agno: "Function X not found" — only emitted when the runtime rejects a tool name
        "ValidationError",  # pydantic: invalid args to a real tool
        "Unexpected keyword argument",  # pydantic kwargs mismatch
        "Missing required argument",  # pydantic positional/required mismatch
    )
    for m in messages:
        if getattr(m, "role", None) != "tool":
            continue
        if getattr(m, "tool_call_error", False):
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                errored_ids.add(tcid)
            continue
        content = getattr(m, "content", None)
        if isinstance(content, str) and any(mk in content for mk in error_re_patterns):
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                errored_ids.add(tcid)

    if not errored_ids:
        return list(messages)

    cleaned = []
    for m in messages:
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            kept = [tc for tc in tcs if (tc.get("id") or tc.get("tool_call_id")) not in errored_ids]
            if len(kept) != len(tcs):
                # Build a shallow copy of the message with filtered tool_calls.
                # Falls back to the original if we can't copy cleanly.
                try:
                    import copy as _copy

                    m2 = _copy.copy(m)
                    m2.tool_calls = kept if kept else None
                    cleaned.append(m2)
                    continue
                except Exception:
                    pass
        cleaned.append(m)
    return cleaned


async def _run_accuracy(
    agent: Any,
    case: EvalCase,
    output_text: str,
    judge_model: Any,
) -> tuple[bool, float | None, str, str]:
    """Run Agno AccuracyEval using already-obtained output.

    Returns ``(passed, score, detail, reason)``. ``reason`` is the
    judge's free-text justification (concatenated across iterations) —
    needed to actually understand why the agent under-scored.
    """
    try:
        from agno.eval.accuracy import AccuracyEval

        acc = AccuracyEval(
            agent=agent,
            input=case.input,
            expected_output=case.expected_output,
            model=judge_model,
            additional_guidelines=case.judge_guidelines,
            num_iterations=case.num_iterations,
            print_summary=False,
            print_results=False,
            telemetry=False,
        )
        result = await acc.arun_with_output(
            output=output_text,
            print_summary=False,
            print_results=False,
        )
        if result is None:
            return False, None, "accuracy eval returned None", ""
        score = result.avg_score
        threshold = case.accuracy_threshold
        passed = score >= threshold
        # Concat per-iteration reasons. Usually 1 iter, but support all.
        reasons = []
        for r in getattr(result, "results", None) or []:
            reason = getattr(r, "reason", None)
            if reason:
                reasons.append(reason)
        reason_text = "\n---\n".join(reasons)
        return passed, score, f"score {score:.1f}/{threshold}", reason_text
    except Exception as exc:
        return False, None, f"accuracy eval error: {exc}", ""


async def run_eval_case(
    case: EvalCase,
    agent: Any,
    judge_model: Any | None,
    session_id: str | None = None,
) -> CaseResult:
    """Run a single eval case against a built agent.

    ``session_id`` should be unique per case — Agno agents load prior
    history from the session DB on every ``arun()`` call, so reusing
    the agent's default session_id across cases pollutes
    ``response.messages`` with tool calls from earlier cases. The
    reliability check then sees a mishmash of tools and reports false
    failures. Pass a fresh UUID per case to isolate.
    """
    result = CaseResult(case=case)
    start = time.monotonic()

    try:
        # Run the agent. If this raises (rate-limit, network, etc.) we
        # capture the error and return a clean ERROR result instead of
        # crashing the whole suite.
        run_kwargs: dict = {"stream": False}
        if session_id is not None:
            run_kwargs["session_id"] = session_id
        try:
            response = await agent.arun(case.input, **run_kwargs)
        except Exception as run_exc:
            result.error = f"agent.arun failed: {run_exc}"
            result.elapsed = time.monotonic() - start
            return result
        output_text = ""
        content = getattr(response, "content", None)
        if isinstance(content, str):
            output_text = content
        elif content is not None:
            output_text = str(content)
        result.response_text = output_text

        # Capture the full tool trace from response.tools (a list of
        # ToolExecution dataclasses). Truncate result previews so the
        # JSON dump stays readable.
        tools = getattr(response, "tools", None) or []
        for t in tools:
            tname = getattr(t, "tool_name", None)
            if not tname:
                continue
            raw_result = getattr(t, "result", None)
            preview = ""
            if raw_result is not None:
                s = str(raw_result)
                preview = s if len(s) <= 400 else s[:397] + "..."
            result.tool_trace.append(
                {
                    "name": tname,
                    "args": getattr(t, "tool_args", None),
                    "result_preview": preview,
                    "error": bool(getattr(t, "tool_call_error", False)),
                }
            )

        all_passed = True

        # 1. ReliabilityEval — expected tool calls (allowlist)
        if case.expected_tool_calls:
            expanded = _expand_tool_names(case.expected_tool_calls)
            passed, detail = await _run_reliability(response, expanded)
            result.reliability_passed = passed
            result.reliability_detail = detail
            if not passed:
                all_passed = False

        # 2. Unexpected tool calls (blocklist — custom check)
        if case.unexpected_tool_calls:
            expanded = _expand_tool_names(case.unexpected_tool_calls)
            passed, detail = check_unexpected_tool_calls(response, expanded)
            result.unexpected_passed = passed
            result.unexpected_detail = detail
            if not passed:
                all_passed = False

        # 3. AccuracyEval — output quality
        if case.expected_output and judge_model:
            passed, score, detail, reason = await _run_accuracy(
                agent,
                case,
                output_text,
                judge_model,
            )
            result.accuracy_passed = passed
            result.accuracy_score = score
            result.accuracy_reason = reason
            if not passed:
                all_passed = False

        # 4. File assertions
        if case.file_assertions:
            for assertion in case.file_assertions:
                passed, detail = check_file_assertion(assertion)
                result.file_results.append((assertion.get("type", ""), passed, detail))
                if not passed:
                    all_passed = False

        result.passed = all_passed

    except Exception as exc:
        result.error = str(exc)
        result.passed = False

    result.elapsed = time.monotonic() - start
    return result
