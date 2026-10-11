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

``update_user_memory`` is the second instance (SWU-105) and is pinned to the
same standard at the bottom of this file. It is the harder of the two: the
capabilities are independent, so the persistence section has four renderings
rather than two, and the one with neither surface has to take its own heading
with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.prompts import load_prompt
from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder

# The names the main-agent prompt uses for the artifact store.
_KNOWLEDGE_TOOLS = ("artifact_add", "artifact_search")


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


@pytest.mark.parametrize("codeindex", [False, True], ids=["plain", "codeindex"])
class TestTheUserMemoryToolIsTaughtOnlyWhenAttached:
    """SWU-105 — the same defect one tool over.

    ``update_user_memory`` was taught unconditionally, in six places,
    including "replying without calling it is a failure". agno attaches the
    tool only when the session has a LearningMachine *and*
    ``learning.user_memory`` is on, so ``learning.enabled=false``,
    ``user_memory=false`` and any session built without a db were all told
    off for not calling a tool that was never in their manifest.

    The two capabilities are independent, which is the part a marker scheme
    gets wrong quietly: four combinations, and the one with neither has to
    drop the section outright rather than leave a heading over nothing.
    """

    def test_with_the_tool_it_is_taught(self, codeindex):
        for knowledge in (True, False):
            prompt = _build(knowledge=knowledge, codeindex=codeindex, user_memory=True)

            assert "update_user_memory" in prompt

    def test_without_it_the_name_never_appears(self, codeindex):
        """Absent, not hedged — a sentence that mentions the tool at all is a
        sentence a model can act on."""
        for knowledge in (True, False):
            prompt = _build(knowledge=knowledge, codeindex=codeindex, user_memory=False)

            assert "update_user_memory" not in prompt, (
                f"prompt still teaches update_user_memory (knowledge={knowledge})"
            )

    def test_the_knowledge_half_survives_either_way(self, codeindex):
        """The mirror of ``test_the_user_memory_half_survives_either_way``:
        the two surfaces are taught in one breath, so a cut aimed at one must
        not take the other."""
        for user_memory in (True, False):
            prompt = _build(knowledge=True, codeindex=codeindex, user_memory=user_memory)

            for tool in _KNOWLEDGE_TOOLS:
                assert tool in prompt

    def test_with_neither_the_section_goes_too(self, codeindex):
        """Nothing to persist to, so "Persisting What You Learn" followed by
        a list of things not to persist is pure noise — and the "never
        persist" bar only means anything next to a tool that could."""
        prompt = _build(knowledge=False, codeindex=codeindex, user_memory=False)

        assert "Persisting What You Learn" not in prompt
        assert "When NOT to persist" not in prompt

    def test_the_absent_branch_says_what_to_do_instead(self, codeindex):
        """Same standard the knowledge-base absent branch is held to: removing
        a tool leaves a question — where does a durable fact about the user go
        now? — and silence gets answered by invention."""
        prompt = _build(knowledge=True, codeindex=codeindex, user_memory=False)

        assert "no user-memory store" in prompt

    def test_no_marker_reaches_the_model_in_any_combination(self, codeindex):
        """Four combinations, and the ``user-memory`` spans nest inside the
        ``knowledge-base`` ones — a nesting mistake shows up as a stray
        marker in exactly one of the four."""
        for knowledge in (True, False):
            for user_memory in (True, False):
                prompt = _build(knowledge=knowledge, codeindex=codeindex, user_memory=user_memory)

                assert "user-memory:" not in prompt
                assert "knowledge-base:" not in prompt
                assert "<!--" not in prompt.replace("<!-- This file gives", "")

    def test_all_four_renders_differ(self, codeindex):
        """Guards the whole scheme against no-opping: a mistyped marker makes
        every absence assertion above pass on a prompt nothing was ever
        marked in."""
        renders = {
            (knowledge, user_memory): _build(
                knowledge=knowledge, codeindex=codeindex, user_memory=user_memory
            )
            for knowledge in (True, False)
            for user_memory in (True, False)
        }

        assert len(set(renders.values())) == 4
        assert len(renders[True, True]) > len(renders[False, False])


class TestTheFlagIsComputedFromRealConfigFields:
    """``learning.enabled AND learning.user_memory`` is the condition the
    builder spells out; if either field is renamed, the expression keeps
    type-checking against a ``SimpleNamespace`` in tests and silently reads
    something else in production."""

    def test_both_fields_exist_on_the_learning_schema(self):
        from ember_code.core.config.schemas.llm_behavior import LearningConfig

        assert {"enabled", "user_memory"} <= set(LearningConfig.model_fields)

    def test_user_memory_false_means_agno_attaches_no_tool(self):
        """The second half of the conjunction, at its source: with the flag
        off ``to_user_memory_input`` returns ``False`` and the learning store
        contributes no ``update_user_memory``."""
        from ember_code.core.config.schemas.llm_behavior import LearningConfig

        assert LearningConfig(user_memory=False).to_user_memory_input() is False
        assert LearningConfig(user_memory=True).to_user_memory_input() is not False
