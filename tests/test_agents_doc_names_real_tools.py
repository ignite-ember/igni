"""Every ``tools:`` list in ``docs/AGENTS.md`` names tools that exist.

The page is the instructions for authoring an agent, and its examples declared
``Glob, Grep, LS, Read, NotebookRead`` — none of which resolve. It also stated
that igni "uses the same tool names as Claude Code" and that agent files are
"fully cross-compatible". They are not: the registry names are ``Write``,
``Edit``, ``Bash``/``BashOutput``, ``WebSearch``, ``WebFetch``, ``Schedule``,
``NotebookEdit``, plus ``CodeIndex`` and ``Visualize`` for the main agent only.

An unknown name in ``tools:`` is dropped rather than rejected, so following the
page produced an agent missing every tool it asked for, and the only symptom was
an agent that turned out to be bad at its job.

Derived from the catalog rather than restated here, so the day a tool is added
or renamed this asserts against the new truth instead of a copy of the old one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ember_code.core.tools.tool_spec import ToolSpecCatalog

DOC = Path(__file__).resolve().parents[1] / "docs" / "AGENTS.md"


def _registry_names() -> set[str]:
    catalog = ToolSpecCatalog.default()
    return {s.name for s in catalog.specs} | {a for s in catalog.specs for a in s.aliases}


def _declared_tool_lines() -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(DOC.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip().startswith("tools:")
    ]


_LINES = _declared_tool_lines()


def test_the_doc_has_examples_to_check():
    assert len(_LINES) > 5, "no `tools:` lines found — the glob or the doc moved"


@pytest.mark.parametrize("lineno,line", _LINES, ids=[str(n) for n, _ in _LINES])
def test_an_example_only_names_real_tools(lineno: int, line: str):
    real = _registry_names()
    declared = [n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()]
    unknown = [n for n in declared if n not in real]

    assert not unknown, (
        f"docs/AGENTS.md:{lineno} declares {unknown}, which resolve to nothing.\n"
        f"Real registry names: {sorted(real)}.\n"
        "Reads and searches are Bash — there is no Read, Grep, Glob or LS."
    )
