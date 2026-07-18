"""Eval wire/domain schemas — pure Pydantic models, no behaviour.

Every dict-shaped wire type the runner used to consume lives here now.
Keeping schemas in one place makes it hard to accidentally sprout a
new list[dict] contract in the runner code (Rule 1) — the class
already exists, use it.

The old runner module re-exports :class:`ToolTraceEntry`,
:class:`CaseResult` and :class:`SuiteResult` from here so external
imports (``from ember_code.core.evals.runner import CaseResult``)
keep working without changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FixtureSpec(BaseModel):
    """One fixture entry from an eval suite YAML.

    ``source`` is resolved relative to the suite's ``fixtures_root``
    (``evals/fixtures/`` for shipped datasets or ``.ember/evals/`` for
    user-authored ones). ``target`` is the path inside the per-suite
    work_dir where the fixture should land.
    """

    model_config = ConfigDict(extra="ignore")

    source: str = ""
    target: str = ""


class ToolArgAssertion(BaseModel):
    """One "did the agent call tool X with args Y?" assertion.

    Passes when *some* call to :attr:`tool` has every key/value in
    :attr:`args_must_contain` present in its captured ``args`` dict.
    Used for verifying enum-like choices — e.g. ``spawn_team`` called
    with ``mode: coordinate`` rather than ``broadcast``.
    """

    model_config = ConfigDict(extra="ignore")

    tool: str = ""
    args_must_contain: dict[str, Any] = Field(default_factory=dict)


class ToolTraceEntry(BaseModel):
    """One tool invocation captured during an eval run.

    ``args`` stays typed as ``dict[str, Any] | None`` because the
    wire shape from Agno's ``ToolExecution.tool_args`` is a free-form
    JSON object — we don't own that schema, and every tool has a
    different args model. ``result_preview`` is truncated to 400
    chars in :class:`AgnoResponseAdapter` before landing here.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any] | None = None
    result_preview: str = ""
    error: bool = False


class FileCheckResult(BaseModel):
    """Result of a single file-assertion check.

    Replaces the old ``tuple[str, bool, str]`` return so reporter /
    JSON consumers see named fields rather than positional indices.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    passed: bool
    detail: str


class CheckResult(BaseModel):
    """Unified return type for every assertion driver's ``run()``.

    Older drivers returned a mix of ``tuple[bool, str]`` and
    ``tuple[bool, float | None, str, str]``. Unifying on one Pydantic
    model kills the positional-unpack hazard at every call site.

    ``score`` is only populated by :class:`AccuracyDriver`.
    ``reason`` carries the LLM judge's free-text justification when
    the driver runs an LLM judge; empty otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str = ""
    score: float | None = None
    reason: str = ""


class AgnoRunOptions(BaseModel):
    """Options passed to ``agent.arun(...)`` as ``**splat`` kwargs.

    ``session_id`` is None when the caller wants Agno's default
    session (per-agent). Cases pass a fresh UUID per run to isolate
    history so ReliabilityEval doesn't see tool calls from earlier
    cases replayed as ``from_history=True`` messages.
    """

    model_config = ConfigDict(extra="forbid")

    stream: bool = False
    session_id: str | None = None

    def dump(self) -> dict[str, Any]:
        """Return only fields that have been set (drops ``session_id=None``)."""
        out: dict[str, Any] = {"stream": self.stream}
        if self.session_id is not None:
            out["session_id"] = self.session_id
        return out


class EvalCase(BaseModel):
    """A single test case within an eval suite.

    For multi-turn cases, set ``prior_messages`` — those are sent
    sequentially on the same session_id BEFORE ``input``. The eval
    judges only the agent's response to ``input`` (the last turn),
    but tool/accuracy checks run against that final response. Use
    this to verify the agent actually carries context across turns
    (e.g. user states a preference in turn 1, the case fails if the
    agent ignores it in turn 3).
    """

    name: str
    input: str
    description: str = ""
    expected_tool_calls: list[str] | None = None
    unexpected_tool_calls: list[str] | None = None
    expected_output: str | None = None
    accuracy_threshold: float = 7.0
    judge_guidelines: str | None = None
    num_iterations: int = 1
    # File assertions — validated per-entry at load time. Kept as a
    # heterogeneous ``list[Any]`` on the model because the assertions
    # module owns the FileAssertion type and importing it here creates
    # a cycle; the loader validates each entry via
    # :class:`FileAssertion.model_validate` before it lands here.
    file_assertions: list[Any] | None = None
    # Prior turns — sent in order on the same session before ``input``.
    # Empty/missing means the case is single-shot (the common case).
    prior_messages: list[str] = Field(default_factory=list)
    # Optional per-case timeout override (seconds). Use for long
    # tasks-mode / multi-specialist orchestration cases that legitimately
    # take longer than the suite default. ``None`` means use the runner's
    # default ``--case-timeout``.
    case_timeout: float | None = None
    # Per-tool argument assertions. Passes when at least ONE call to
    # ``tool`` has args containing every ``args_must_contain`` key/value
    # pair. Use for verifying that the agent picked the right enum value
    # (e.g. ``spawn_team`` with ``mode: coordinate`` rather than
    # ``broadcast``).
    tool_arg_assertions: list[ToolArgAssertion] | None = None


class EvalSuite(BaseModel):
    """A collection of eval cases targeting one agent."""

    agent: str
    description: str = ""
    fixtures: list[FixtureSpec] | None = None
    # Optional dotted path to a Python module that exposes
    # ``async def setup(work_dir: Path) -> None``. Called after
    # fixture files are copied but before any case runs. Used by the
    # codeindex eval to git-init the work_dir and apply a JSONL
    # changeset to chroma so the agent sees a populated index when
    # it starts. Suite passes if the import path is empty / missing.
    setup_module: str | None = None
    cases: list[EvalCase] = Field(default_factory=list)

    @classmethod
    def load_all(cls, project_dir: Path) -> list[EvalSuite]:
        """Discover and load all eval suites.

        Delegates to :func:`ember_code.core.evals.loader.load_all_suites`
        to avoid importing YAML parsing code from this pure-schema module.
        """
        from ember_code.core.evals.loader import load_all_suites

        return load_all_suites(project_dir)


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
    tool_trace: list[ToolTraceEntry] = Field(default_factory=list)
    """Order matches Agno's call order. Migrated to :class:`ToolTraceEntry`
    for Rule-1 compliance; ``model_dump()`` on the ``CaseResult`` serialises
    each entry back to the same dict shape the previous ``list[dict]``
    produced, so downstream JSON reporters see no shape change."""

    # ── Per-check results ────────────────────────────────────────────
    reliability_passed: bool | None = None
    reliability_detail: str = ""
    unexpected_passed: bool | None = None
    unexpected_detail: str = ""
    accuracy_score: float | None = None
    accuracy_passed: bool | None = None
    accuracy_reason: str = ""
    """The LLM judge's free-text reasoning. Empty when no judge ran."""
    file_results: list[FileCheckResult] = Field(default_factory=list)
    tool_arg_passed: bool | None = None
    tool_arg_detail: str = ""

    error: str | None = None
    elapsed: float = 0.0

    def add_file_result(self, r: FileCheckResult) -> None:
        """Append one file-check outcome."""
        self.file_results.append(r)

    def compute_passed(self) -> None:
        """Derive :attr:`passed` from the individual per-check fields.

        A case passes when every check that ran returned True. Checks
        that were skipped (``*_passed is None``) don't count. Any file
        assertion failing drags the case down. Kills the scattered
        ``all_passed`` local that used to live in ``_apply_case_assertions``.
        """
        checks: list[bool | None] = [
            self.reliability_passed,
            self.unexpected_passed,
            self.accuracy_passed,
            self.tool_arg_passed,
        ]
        for c in checks:
            if c is False:
                self.passed = False
                return
        for fr in self.file_results:
            if not fr.passed:
                self.passed = False
                return
        self.passed = True

    # ── Presentation helpers (Markdown reporter) ──────────────────────
    # These live on the model so the renderer is a thin coordinator and
    # the report's per-case formatting can't drift from the data it
    # reads. Each returns raw Markdown fragments; the renderer joins.

    def status_badges(self) -> list[str]:
        """Return the per-check badges for this case.

        Only emits a badge when the corresponding check actually ran
        (e.g. accuracy is omitted when the case has no expected output
        and so :attr:`accuracy_score` stayed ``None``). File checks
        collapse to one ``files: PASS|FAIL`` badge regardless of how
        many files the case asserts on.
        """
        badges: list[str] = []
        if self.reliability_passed is not None:
            badges.append(f"reliability: {'PASS' if self.reliability_passed else 'FAIL'}")
        if self.accuracy_score is not None:
            badges.append(f"accuracy: {self.accuracy_score:.1f}")
        if self.file_results:
            file_ok = all(r.passed for r in self.file_results)
            badges.append(f"files: {'PASS' if file_ok else 'FAIL'}")
        return badges

    def summary_line(self) -> str:
        """Render the one-line per-case summary used in the report header.

        Format: ``"+ case_name                 elapsed_time  [badge1, badge2]"``
        (badges section omitted entirely when no checks ran).
        """
        badges = self.status_badges()
        badge_str = f"  [{', '.join(badges)}]" if badges else ""
        symbol = "+" if self.passed else "x"
        return f"  {symbol} {self.case.name:<35} {self.elapsed:.1f}s{badge_str}"

    def failure_detail_lines(self) -> list[str]:
        """Return indented detail lines for each failed check.

        Used by the report only when :attr:`passed` is False; the
        renderer still calls it unconditionally and the list ends up
        empty on success, so the call site is unconditional.
        """
        lines: list[str] = []
        if self.error:
            lines.append(f"    error: {self.error}")
        if self.reliability_passed is False:
            lines.append(f"    reliability: {self.reliability_detail}")
        if self.unexpected_passed is False:
            lines.append(f"    {self.unexpected_detail}")
        if self.accuracy_passed is False:
            lines.append(f"    accuracy: score {self.accuracy_score or '?'}")
        for fr in self.file_results:
            if not fr.passed:
                lines.append(f"    {fr.type}: {fr.detail}")
        return lines


class SuiteResult(BaseModel):
    """Result of running all cases in an eval suite.

    Pure wire schema — no execution behaviour. Suite execution lives
    on :class:`ember_code.core.evals.runner.SuiteRunner`.
    """

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
