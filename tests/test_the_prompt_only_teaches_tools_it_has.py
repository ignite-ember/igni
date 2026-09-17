"""The system prompt taught four tools the session often did not attach.

``main_agent.md`` spent ~30 lines on ``knowledge_add`` / ``knowledge_search`` —
including "search the knowledge base *before* falling back to grep" — while
``ToolsBuilder.knowledge()`` returns ``[]`` unless an index is configured. The
model follows the instruction it was given, emits a call that is not in its
manifest, and gets an error for a capability the prompt promised.

It is not hypothetical. Across the capture corpus: **7,459 system prompts teach
those tools, 0 manifests carry them, 0 calls were ever made** — the discipline
was taught to a model that could not act on it. Same failure as PRD-12's
``file_read``, an order of magnitude larger.

The fix is a conditional span in the markdown and one flag read off the same
attribute the toolkit reads. What this file pins is the *equality* of those two
conditions, in both directions, plus the two ways a marker scheme fails
silently: a stray marker reaching the model, and a span that disappears whatever
the flag says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.prompts import load_prompt
from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder

_KNOWLEDGE_TOOLS = ("knowledge_add", "knowledge_search", "knowledge_delete", "knowledge_status")


class _Settings:
    """Enough settings for the project-map phase to look and find nothing.

    The codeindex variant of the prompt is the one that consults them, and it
    is half of what this file is about — the two prompt files drifted apart
    once already."""

    class storage:  # noqa: N801
        data_dir = Path("/nonexistent")


def _build(*, knowledge: bool, codeindex: bool = False, user_memory: bool = True) -> str:
    return PromptBuilder(
        project_dir=Path("/nonexistent"),
        settings=_Settings,
        codeindex_available=codeindex,
        knowledge_available=knowledge,
        user_memory_available=user_memory,
        agent_catalog="- researcher: reads things",
        skill_descriptions="",
        prompt_loader=load_prompt,
    ).render()


@pytest.mark.parametrize("codeindex", [False, True], ids=["plain", "codeindex"])
class TestTheTwoConditionsAgree:
    def test_with_a_knowledge_base_the_tools_are_taught(self, codeindex):
        prompt = _build(knowledge=True, codeindex=codeindex)

        for tool in _KNOWLEDGE_TOOLS:
            assert tool in prompt, f"{tool} is attached but no longer taught"

    def test_without_one_they_are_not_mentioned_at_all(self, codeindex):
        """Not "mentioned more carefully" — absent. A hedged sentence still
        puts the tool name in front of a model that cannot call it."""
        prompt = _build(knowledge=False, codeindex=codeindex)

        offenders = [tool for tool in _KNOWLEDGE_TOOLS if tool in prompt]
        assert not offenders, f"prompt still teaches {offenders} with no knowledge base"

    def test_the_user_memory_half_survives_either_way(self, codeindex):
        """The two surfaces are described together, so a crude cut would take
        ``update_user_memory`` with it — and that tool is always attached."""
        for knowledge in (True, False):
            prompt = _build(knowledge=knowledge, codeindex=codeindex)
            assert "update_user_memory" in prompt
            assert "Persisting What You Learn" in prompt

    def test_no_marker_reaches_the_model(self, codeindex):
        for knowledge in (True, False):
            prompt = _build(knowledge=knowledge, codeindex=codeindex)
            assert "knowledge-base:" not in prompt
            assert "<!--" not in prompt.replace("<!-- This file gives", "")

    def test_the_absent_branch_says_what_to_do_instead(self, codeindex):
        """Removing the tool leaves a real question — where does a project
        convention go now? A prompt that only deletes guidance answers it with
        silence, and the model invents something."""
        prompt = _build(knowledge=False, codeindex=codeindex)

        assert "no knowledge base" in prompt.lower()

    def test_the_two_renders_actually_differ(self, codeindex):
        """Guards against the whole scheme quietly no-opping: if the markers
        were mistyped, every assertion above about absence would also pass on
        a prompt where nothing was ever marked."""
        with_kb = _build(knowledge=True, codeindex=codeindex)
        without = _build(knowledge=False, codeindex=codeindex)

        assert with_kb != without
        assert len(with_kb) > len(without)


class TestTheResolverFailsLoudly:
    def _builder(self, *, knowledge: bool, user_memory: bool = True) -> PromptBuilder:
        return PromptBuilder(
            project_dir=Path("/nonexistent"),
            settings=_Settings,
            codeindex_available=False,
            knowledge_available=knowledge,
            user_memory_available=user_memory,
            agent_catalog="",
            skill_descriptions="",
            prompt_loader=load_prompt,
        )

    def test_an_unclosed_span_raises(self):
        """The alternative is a prompt that silently loses everything after the
        opening marker, which is the exact class of bug this row is about."""
        broken = "intro\n<!-- knowledge-base:only -->\nsome guidance\n"

        with pytest.raises(ValueError, match="unbalanced"):
            self._builder(knowledge=True).resolve_conditionals(broken)

    def test_an_inline_span_is_cut_without_eating_its_sentence(self):
        """Some spans are mid-sentence: "without calling `update_user_memory`
        or `knowledge_add`". Dropping the clause must leave the sentence."""
        text = (
            "respond without calling `update_user_memory`"
            "<!-- knowledge-base:only --> or `knowledge_add`<!-- /knowledge-base:only -->."
        )

        assert self._builder(knowledge=False).resolve_conditionals(text) == (
            "respond without calling `update_user_memory`."
        )
        assert self._builder(knowledge=True).resolve_conditionals(text) == (
            "respond without calling `update_user_memory` or `knowledge_add`."
        )
