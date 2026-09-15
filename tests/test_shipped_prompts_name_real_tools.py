"""A shipped prompt may not instruct a tool the model will never be given.

Two of these shipped at once, and neither was catchable by running the app:

* The main prompt documented ``codeindex_query`` and ``codeindex_tree`` long
  after ``tool.py`` stopped registering them, leaving ``codeindex_cypher`` as
  the only CodeIndex surface.
* The plan-mode prompt instructed ``file_read``, which is not a tool at all —
  it is a *permission category*. No manifest has ever contained it.

Both fail silently and expensively. The model does what the prompt says, emits a
call for a function that is not in its manifest, and gets an error back for a
capability the prompt promised it had. Then it improvises. A prompt is the one
part of the system that is never type-checked and never imported, so nothing
else in the repo would notice.

The retired list is two halves, and only one of them could be written by hand:

* **Derived.** A spec can construct its toolkit with ``enable_<fn>=False``. The
  ``Write`` spec builds agno's ``FileTools`` that way, keeping ``save_file`` and
  switching off ``read_file``, ``list_files``, ``search_files``,
  ``search_content``, ``read_file_chunk`` and ``replace_file_chunk``. Those
  methods exist on the class, so anything that reads the class — a human, or a
  name-collecting script — concludes they are available. Reading the switches is
  the only way to know they are not, and it keeps working when the set changes.
* **Named.** Tools deleted outright leave nothing behind to derive from. Once
  ``codeindex_query`` is gone from the registry there is no object to ask, so
  the names are listed here with the date and reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ember_code.core.tools.tool_spec import ToolSpecCatalog

PROMPT_ROOTS = (
    "src/ember_code/core/prompts",
    "src/ember_code/bundled_agents",
    "src/ember_code/bundled_skills",
)

#: Removed from the registry, so nothing is left to derive them from.
RETIRED_BY_DELETION: dict[str, str] = {
    "codeindex_query": "replaced by codeindex_cypher; the structured surface is gone",
    "codeindex_tree": "replaced by codeindex_cypher",
    "file_read": "never a tool — it is a permission category, and reads as one in a prompt",
}


def _disabled_by_spec() -> dict[str, str]:
    """Functions a spec explicitly switches off when building its toolkit."""
    specs = list(ToolSpecCatalog.default().specs)
    off: dict[str, str] = {}
    on: set[str] = set()
    for spec in specs:
        for key, value in (spec.static_kwargs or {}).items():
            if not key.startswith("enable_"):
                continue
            name = key[len("enable_") :]
            (on.add(name) if value else off.setdefault(name, spec.name))
    granted = {n for spec in specs for n in spec.agno_function_names}
    return {
        n: f"switched off by the {owner!r} spec"
        for n, owner in off.items()
        if n not in on and n not in granted
    }


def _prompt_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    out: list[Path] = []
    for rel in PROMPT_ROOTS:
        out.extend(sorted((root / rel).rglob("*.md")))
    return out


_FILES = _prompt_files()


def test_there_are_prompts_to_check():
    """A glob that matches nothing would make every case below vacuous — the
    same failure mode as the tool enumerator that silently lost a module."""
    assert len(_FILES) > 5, [str(p) for p in _FILES]


def test_the_derivation_finds_the_switches():
    """If the spec shape changes so nothing is derived, this file quietly
    degrades to the hand-written half. Fail loudly instead."""
    assert "read_file" in _disabled_by_spec()


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_a_prompt_does_not_instruct_a_tool_that_is_not_there(path: Path):
    retired = {**RETIRED_BY_DELETION, **_disabled_by_spec()}
    text = path.read_text(encoding="utf-8")

    # Only backticked mentions. These prompts discuss reading files in prose
    # constantly ("read what the searches surface"), and a bare-word match would
    # flag every one of them. A name in backticks is the prompt telling the
    # model what to call.
    quoted = set(re.findall(r"`([a-z_][a-z0-9_]*)`", text))
    offenders = sorted(quoted & set(retired))

    assert not offenders, "\n".join(
        [f"{path.name} instructs {len(offenders)} tool(s) the model is not given:"]
        + [f"  `{name}` — {retired[name]}" for name in offenders]
        + ["Use `run_shell_command` for file reads, or `codeindex_cypher` for the graph."]
    )
