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
CODEINDEX_PROMPTS = sorted((REPO / "agents").glob("*.codeindex.md"))

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


def test_there_are_codeindex_prompts_to_check():
    assert CODEINDEX_PROMPTS, "no *.codeindex.md found — the glob or the layout moved"


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=lambda p: p.name)
def test_every_codeindex_prompt_carries_the_placeholder(path: Path):
    assert CODEINDEX_SCHEMA_PLACEHOLDER in path.read_text(), (
        f"{path.name} teaches an agent to query the index but never receives the "
        "schema. That is how the shipped prompts came to know about none of the "
        "counted facts."
    )


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=lambda p: p.name)
def test_the_loaded_prompt_knows_the_counted_facts(path: Path):
    """The substitution has to happen where agents are actually built."""
    body = AgentMarkdownFile(path).parse().system_prompt
    assert CODEINDEX_SCHEMA_PLACEHOLDER not in body, "placeholder survived into the prompt"
    missing = [fact for fact in COUNTED_FACTS if fact not in body]
    assert not missing, f"{path.name} does not mention {missing}"


@pytest.mark.parametrize("path", CODEINDEX_PROMPTS, ids=lambda p: p.name)
def test_no_prompt_hand_writes_the_schema_again(path: Path):
    """A second copy is what drifted the first time.

    A prompt may explain *how* to investigate; the property list comes from the
    substitution. ``### :Item`` was the heading of the copy that went stale.
    """
    text = path.read_text()
    assert "### :Item" not in text, (
        f"{path.name} has grown its own :Item property list again — delete it and "
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
