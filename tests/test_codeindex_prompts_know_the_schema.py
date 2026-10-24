"""Every CodeIndex-aware prompt must carry the live graph schema, not a copy.

The schema was maintained twice. ``GRAPH_SCHEMA_DESCRIPTION`` is the version the
evaluation injected and measured at 85.8% against ripgrep's 64.5%; the version
the product actually shipped was hand-written inside
``agents/data-architect.codeindex.md``, and it knew about none of the counted
facts that produced the score — no ``fan_in``, no ``method_count``, no
``is_callable``, no ``sink_hits``, no term search — while still advertising two
properties that are empty on all 86,332 indexed items.

Nothing failed. The prompts were coherent, the tests passed, and the agent simply
never asked for anything the index had learned to answer. So these tests assert
the plumbing (the placeholder is present and substituted) and the absence of a
second copy (no prompt hand-writes the property list again).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.code_index.neo4j_schema import GRAPH_SCHEMA_DESCRIPTION
from ember_code.core.prompts import (
    CODEINDEX_SCHEMA_PLACEHOLDER,
    inject_codeindex_schema,
    load_prompt,
)

REPO = Path(__file__).resolve().parents[1]

# The prompts that query the index are CodeIndex's: they ship in its
# extension's plugin as ``<agent>.codeindex.md`` and replace igni's agents
# while it runs. What igni owns is the contract they depend on — the
# placeholder, and the schema substituted into it where agents are built —
# so they are checked here, against the sibling checkout when there is one.
CODEINDEX_AGENT_DIR = REPO.parent / "igni__code-index" / "extension" / "plugin" / "agents"
CODEINDEX_PROMPTS = sorted(CODEINDEX_AGENT_DIR.glob("*.codeindex.md"))
_needs_codeindex = pytest.mark.skipif(
    not CODEINDEX_AGENT_DIR.is_dir(), reason="no igni__code-index checkout beside this one"
)


def _prompt_id(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


# Facts the index gained after the shipped prompts were written. Each one is the
# whole answer to a question the evaluation measured, so a prompt that does not
# mention it cannot ask for it.
COUNTED_FACTS = [
    "fan_in",
    "importer_count",
    "test_importer_count",
    "member_count",
    "method_count",
    "is_callable",
    "sink_hits",
    "empty_handlers",
    "subproject",
]


@_needs_codeindex
def test_there_are_codeindex_prompts_to_check():
    """An empty glob here passes every parametrised test below without
    checking anything CodeIndex ships."""
    assert CODEINDEX_PROMPTS, f"no *.codeindex.md under {CODEINDEX_AGENT_DIR}"


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=_prompt_id)
def test_every_codeindex_prompt_carries_the_placeholder(path: Path):
    assert CODEINDEX_SCHEMA_PLACEHOLDER in path.read_text(), (
        f"{_prompt_id(path)} teaches an agent to query the index but never receives the "
        "schema. That is how the shipped prompts came to know about none of the "
        "counted facts."
    )


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=_prompt_id)
def test_the_loaded_prompt_knows_the_counted_facts(path: Path):
    """The substitution has to happen where agents are actually built."""
    body = AgentMarkdownFile(path).parse().system_prompt
    assert CODEINDEX_SCHEMA_PLACEHOLDER not in body, "placeholder survived into the prompt"
    missing = [fact for fact in COUNTED_FACTS if fact not in body]
    assert not missing, f"{_prompt_id(path)} does not mention {missing}"


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=_prompt_id)
def test_no_prompt_hand_writes_the_schema_again(path: Path):
    """A second copy is what drifted the first time.

    A prompt may explain *how* to investigate; the property list comes from the
    substitution. ``### :Item`` was the heading of the copy that went stale.
    """
    text = path.read_text()
    assert "### :Item" not in text, (
        f"{_prompt_id(path)} has grown its own :Item property list again — delete it and "
        "rely on the placeholder"
    )


def test_the_main_agent_prompt_is_substituted_too():
    body = load_prompt("main_agent.codeindex")
    assert CODEINDEX_SCHEMA_PLACEHOLDER not in body
    # The main agent does not author Cypher, it delegates — so it needs to know
    # which questions are now index questions rather than shell questions.
    for phrase in ("sink_hits", "empty_handlers", "importer_count"):
        assert phrase in body, f"main agent prompt never mentions {phrase}"


class TestInjection:
    def test_it_is_a_no_op_without_the_placeholder(self):
        assert inject_codeindex_schema("plain prompt") == "plain prompt"

    def test_it_substitutes_the_live_schema(self):
        out = inject_codeindex_schema(f"before\n{CODEINDEX_SCHEMA_PLACEHOLDER}\nafter")
        assert GRAPH_SCHEMA_DESCRIPTION in out
        assert out.startswith("before")
        assert out.endswith("after")

    def test_every_occurrence_is_replaced(self):
        out = inject_codeindex_schema(
            f"{CODEINDEX_SCHEMA_PLACEHOLDER} and {CODEINDEX_SCHEMA_PLACEHOLDER}"
        )
        assert CODEINDEX_SCHEMA_PLACEHOLDER not in out
