"""Eval loader — parse YAML eval files into structured data."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
    file_assertions: list[dict] | None = None
    # Prior turns — sent in order on the same session before ``input``.
    # Empty/missing means the case is single-shot (the common case).
    prior_messages: list[str] = Field(default_factory=list)
    # Optional per-case timeout override (seconds). Use for long
    # tasks-mode / multi-specialist orchestration cases that legitimately
    # take longer than the suite default. ``None`` means use the runner's
    # default ``--case-timeout``.
    case_timeout: float | None = None
    # Per-tool argument assertions. Each entry: {tool, args_must_contain}.
    # Passes when at least ONE call to ``tool`` has args containing every
    # ``args_must_contain`` key/value pair. Use for verifying that the
    # agent picked the right enum value (e.g. ``spawn_team`` with
    # ``mode: coordinate`` rather than ``broadcast``).
    tool_arg_assertions: list[dict] | None = None


class EvalSuite(BaseModel):
    """A collection of eval cases targeting one agent."""

    agent: str
    description: str = ""
    fixtures: list[dict] | None = None
    # Optional dotted path to a Python module that exposes
    # ``async def setup(work_dir: Path) -> None``. Called after
    # fixture files are copied but before any case runs. Used by the
    # codeindex eval to git-init the work_dir and apply a JSONL
    # changeset to chroma so the agent sees a populated index when
    # it starts. Suite passes if the import path is empty / missing.
    setup_module: str | None = None
    cases: list[EvalCase] = Field(default_factory=list)

    @classmethod
    def load_all(cls, project_dir: Path) -> list["EvalSuite"]:
        """Discover and load all eval suites.

        Looks in two locations:
          - ``<project_dir>/evals/`` — committed, for built-in agent
            datasets shipped with the repo (e.g. ember-code's own evals).
          - ``<project_dir>/.ember/evals/`` — local user-authored evals
            (gitignored), for custom agents in the user's project.

        Both are merged. Same-named files in ``.ember/evals/`` win on
        conflict — the user's local copy overrides the shipped one.
        """
        suites: list[EvalSuite] = []
        seen_files: set[str] = set()

        for evals_dir in (project_dir / ".ember" / "evals", project_dir / "evals"):
            if not evals_dir.is_dir():
                continue
            for path in sorted(evals_dir.glob("*.yaml")):
                if path.name in seen_files:
                    continue
                seen_files.add(path.name)
                suite = load_eval_file(path)
                if suite:
                    suites.append(suite)
        return suites


def _parse_case(data: dict) -> EvalCase:
    """Parse a single case dict from YAML."""
    return EvalCase(
        name=data["name"],
        input=data["input"],
        description=data.get("description", ""),
        expected_tool_calls=data.get("expected_tool_calls"),
        unexpected_tool_calls=data.get("unexpected_tool_calls"),
        expected_output=data.get("expected_output"),
        accuracy_threshold=data.get("accuracy_threshold", 7.0),
        judge_guidelines=data.get("judge_guidelines"),
        num_iterations=data.get("num_iterations", 1),
        file_assertions=data.get("file_assertions"),
        prior_messages=data.get("prior_messages") or [],
        case_timeout=data.get("case_timeout"),
        tool_arg_assertions=data.get("tool_arg_assertions"),
    )


def load_eval_file(path: Path) -> EvalSuite | None:
    """Load a single YAML eval file into an EvalSuite."""
    try:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or "agent" not in data or "cases" not in data:
            logger.warning("Skipping invalid eval file: %s", path)
            return None

        cases = [_parse_case(c) for c in data["cases"] if "name" in c and "input" in c]
        return EvalSuite(
            agent=data["agent"],
            description=data.get("description", ""),
            fixtures=data.get("fixtures"),
            setup_module=data.get("setup_module"),
            cases=cases,
        )
    except Exception as exc:
        logger.warning("Failed to load eval file %s: %s", path, exc)
        return None
