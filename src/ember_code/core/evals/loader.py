"""Eval loader — parse YAML eval files into structured schemas.

The Pydantic models (:class:`EvalCase`, :class:`EvalSuite`) live in
:mod:`ember_code.core.evals.schemas` — this module only handles the
disk/YAML side. Re-exports the models for back-compat with existing
imports (``from ember_code.core.evals.loader import EvalCase``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ember_code.core.evals.assertions import FileAssertion
from ember_code.core.evals.schemas import (
    EvalCase,
    EvalSuite,
    FixtureSpec,
    ToolArgAssertion,
)

logger = logging.getLogger(__name__)


__all__ = [
    "EvalCase",
    "EvalSuite",
    "load_eval_file",
    "load_all_suites",
]


class _EvalYamlParser:
    """Parses one YAML file into an :class:`EvalSuite`.

    Encapsulates the per-entry validation that used to sit as a free
    function ``_parse_case`` at module scope. Keeping it as a class
    lets tests inject a stub parser (e.g. one that always returns None
    for a bad-fixture path) without monkeypatching module globals.
    """

    def parse_file(self, path: Path) -> EvalSuite | None:
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            logger.warning("Failed to load eval file %s: %s", path, exc)
            return None

        if not isinstance(data, dict) or "agent" not in data or "cases" not in data:
            logger.warning("Skipping invalid eval file: %s", path)
            return None

        try:
            cases = [
                self._parse_case(c)
                for c in data["cases"]
                if isinstance(c, dict) and "name" in c and "input" in c
            ]
            fixtures = self._parse_fixtures(data.get("fixtures"))
            return EvalSuite(
                agent=data["agent"],
                description=data.get("description", ""),
                fixtures=fixtures,
                setup_module=data.get("setup_module"),
                cases=cases,
            )
        except ValidationError as exc:
            logger.warning("Eval file %s failed validation: %s", path, exc)
            return None
        except Exception as exc:
            logger.warning("Failed to load eval file %s: %s", path, exc)
            return None

    def _parse_case(self, data: dict[str, Any]) -> EvalCase:
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
            file_assertions=self._parse_file_assertions(data.get("file_assertions")),
            prior_messages=data.get("prior_messages") or [],
            case_timeout=data.get("case_timeout"),
            tool_arg_assertions=self._parse_tool_arg_assertions(
                data.get("tool_arg_assertions"),
            ),
        )

    @staticmethod
    def _parse_fixtures(raw: list[Any] | None) -> list[FixtureSpec] | None:
        if not raw:
            return None
        out: list[FixtureSpec] = []
        for entry in raw:
            if isinstance(entry, FixtureSpec):
                out.append(entry)
            elif isinstance(entry, dict):
                out.append(FixtureSpec.model_validate(entry))
        return out

    @staticmethod
    def _parse_file_assertions(raw: list[Any] | None) -> list[FileAssertion] | None:
        if not raw:
            return None
        out: list[FileAssertion] = []
        for entry in raw:
            if isinstance(entry, FileAssertion):
                out.append(entry)
            elif isinstance(entry, dict):
                # Validate at load time so a typo'd ``type`` fails here
                # (with the whole file logged and skipped) rather than
                # producing a run-time "unknown assertion type" per case.
                out.append(FileAssertion.model_validate(entry))
        return out

    @staticmethod
    def _parse_tool_arg_assertions(
        raw: list[Any] | None,
    ) -> list[ToolArgAssertion] | None:
        if not raw:
            return None
        out: list[ToolArgAssertion] = []
        for entry in raw:
            if isinstance(entry, ToolArgAssertion):
                out.append(entry)
            elif isinstance(entry, dict):
                out.append(ToolArgAssertion.model_validate(entry))
        return out


#: Module-level default parser instance — the parser is stateless,
#: so one instance is fine.
_DEFAULT_PARSER = _EvalYamlParser()


def load_eval_file(path: Path) -> EvalSuite | None:
    """Load a single YAML eval file into an :class:`EvalSuite`.

    Thin wrapper around :class:`_EvalYamlParser` — kept as a
    module-level function for back-compat with existing test imports.
    """
    return _DEFAULT_PARSER.parse_file(path)


def load_all_suites(project_dir: Path) -> list[EvalSuite]:
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
