"""An extension may override a prompt, or edit it.

Per prompt — the main prompt, plan mode, any agent — an extension brings
a whole prompt of its own, or appends a section, inserts one before or
after a named section, or replaces a named section. Edits from several
extensions stack, in the order a super admin set; an override is the new
starting point, and the others' edits land on it where it has the
section they name. What this pins is that each of those does what it
says and nothing else, and that igni's own text is what is left when
nothing is installed.

Who may do what when two extensions want the same thing is decided
before anything reaches the table here
(``test_extensions_collide_and_a_super_admin_decides``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.prompts import load_prompt
from ember_code.core.prompts.contributions import (
    EditOp,
    PromptContributions,
    PromptEdit,
    PromptOverride,
)
from ember_code.core.prompts.document import (
    EditRefused,
    Position,
    PromptDocument,
    PromptTarget,
)
from ember_code.core.session.agent_builder.plan_mode_nudge import PlanModeNudge
from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder


class _Settings:
    class storage:  # noqa: N801
        data_dir = Path("/nonexistent")


def _replace(target: str, section: str, text: str, when: str | None = None) -> PromptEdit:
    return PromptEdit(target=target, op=EditOp.REPLACE, section=section, text=text, when=when)


def _insert(target: str, anchor: str, position: Position, name: str, text: str) -> PromptEdit:
    return PromptEdit(
        target=target, op=EditOp.INSERT, section=anchor, position=position, name=name, text=text
    )


def _append(target: str, name: str, text: str) -> PromptEdit:
    return PromptEdit(target=target, op=EditOp.APPEND, name=name, text=text)


def _render(contributions: PromptContributions, *, user_memory: bool = True) -> str:
    return PromptBuilder(
        project_dir=Path("/nonexistent"),
        settings=_Settings,
        contributions=contributions,
        user_memory_available=user_memory,
        agent_catalog="- researcher: reads things",
        skill_descriptions="",
        prompt_loader=load_prompt,
    ).render()


def _composed(contributions: PromptContributions, target: str, base: str) -> str:
    composition = contributions.compose(target, base)
    assert composition is not None
    return composition.document.render()


class TestTheDocument:
    TEXT = (
        "<!-- section:a -->\n## A\nigni's a\n<!-- /section:a -->\n\n"
        "<!-- section:b -->\n## B\nigni's b\n<!-- /section:b -->\n"
    )
    AGENT = "---\nname: x\ndescription: igni's\n---\n\n" + TEXT

    def test_it_reads_its_sections(self):
        assert PromptDocument(self.TEXT).sections() == ["a", "b"]

    def test_an_agent_s_header_is_a_section(self):
        assert PromptDocument(self.AGENT, agent=True).sections() == ["frontmatter", "a", "b"]

    def test_a_replaced_section_is_the_replacement_and_nothing_else(self):
        text = PromptDocument(self.TEXT).replace("a", "## A\nours").render()
        assert "ours" in text and "igni's a" not in text
        assert "igni's b" in text

    def test_an_insert_lands_beside_its_anchor(self):
        text = (
            PromptDocument(self.TEXT)
            .insert("a", Position.AFTER, "x", "after a")
            .insert("b", Position.BEFORE, "y", "before b")
            .render()
        )
        assert text.index("igni's a") < text.index("after a") < text.index("before b")
        assert text.index("before b") < text.index("igni's b")

    def test_inserts_after_one_section_keep_the_order_they_were_made_in(self):
        text = (
            PromptDocument(self.TEXT)
            .insert("a", Position.AFTER, "x", "first")
            .insert("a", Position.AFTER, "y", "second")
            .render()
        )
        assert text.index("igni's a") < text.index("first") < text.index("second")
        assert text.index("second") < text.index("igni's b")

    def test_an_append_lands_at_the_end(self):
        assert PromptDocument(self.TEXT).append("x", "the end").render().endswith("the end")

    def test_an_added_section_is_no_anchor(self):
        """Edits anchor to the prompt they were written against, never to
        another extension's additions."""
        document = PromptDocument(self.TEXT).append("x", "added")
        assert document.sections() == ["a", "b"]
        with pytest.raises(EditRefused):
            document.replace("x", "nope")

    def test_an_agent_s_header_is_replaced_whole(self):
        text = PromptDocument(self.AGENT, agent=True).replace(
            "frontmatter", "---\nname: x\ndescription: ours\n---"
        )
        assert text.render().startswith("---\nname: x\ndescription: ours\n---\n")
        with pytest.raises(EditRefused, match="YAML block"):
            PromptDocument(self.AGENT, agent=True).replace("frontmatter", "description: ours")

    def test_no_marker_survives(self):
        assert "<!--" not in PromptDocument(self.TEXT).append("x", "y").render()

    def test_a_marker_without_its_partner_is_refused(self):
        """A section whose close went missing would swallow the rest of the
        prompt the moment somebody replaced it."""
        with pytest.raises(ValueError, match="never closed"):
            PromptDocument("<!-- section:a -->\nno end")

    def test_a_section_inside_another_is_refused(self):
        with pytest.raises(ValueError, match="opens inside"):
            PromptDocument(
                "<!-- section:a -->\n<!-- section:b -->\n<!-- /section:b -->\n<!-- /section:a -->"
            )

    @pytest.mark.parametrize(
        "spelled",
        [
            "main",
            "main#workflow",
            "plan_mode",
            "agent:explorer",
            "agent:acme.notes:archivist#intro",
        ],
    )
    def test_a_target_spells_back_as_written(self, spelled):
        assert str(PromptTarget.parse(spelled)) == spelled

    @pytest.mark.parametrize("spelled", ["system", "main#Workflow", "main#", "main.workflow"])
    def test_a_target_igni_cannot_have_is_refused(self, spelled):
        with pytest.raises(ValueError):
            PromptTarget.parse(spelled)


class TestTheTable:
    def test_edits_from_several_extensions_apply_in_the_order_set(self):
        contributions = PromptContributions()
        contributions.set_owner("a.ext", "Ay", edits=[_append("main", "a", "from a")])
        contributions.set_owner("b.ext", "Bee", edits=[_append("main", "b", "from b")])
        rendered = _composed(contributions, "main", "x")
        assert rendered.index("from a") < rendered.index("from b")

        contributions.set_order(["b.ext", "a.ext"])
        rendered = _composed(contributions, "main", "x")
        assert rendered.index("from b") < rendered.index("from a")

    def test_an_override_is_the_new_starting_point(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            overrides=[
                PromptOverride(target="main", text="<!-- section:s -->\nours\n<!-- /section:s -->")
            ],
        )
        contributions.set_owner(
            "b.ext",
            "Bee",
            edits=[
                _replace("main", "s", "bee's s"),
                _insert("main", "gone", Position.AFTER, "n", "never"),
                _append("main", "t", "tail"),
            ],
        )
        composition = contributions.compose("main", "igni's")
        assert composition is not None
        rendered = composition.document.render()
        assert "igni's" not in rendered and "ours" not in rendered
        assert "bee's s" in rendered and "tail" in rendered and "never" not in rendered
        assert composition.override_owner == "a.ext"
        [skipped] = composition.skipped
        assert "a.ext's override has no section 'gone'" in skipped

    def test_an_exclusive_override_takes_no_one_else_s_edits(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", overrides=[PromptOverride(target="main", text="ours", exclusive=True)]
        )
        contributions.set_owner("b.ext", "Bee", edits=[_append("main", "t", "tail")])
        composition = contributions.compose("main", "igni's")
        assert composition is not None
        assert composition.document.render() == "ours"
        assert "exclusively" in composition.skipped[0]

    def test_a_condition_holds_only_once_it_is_said(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", overrides=[PromptOverride(target="main", text="ours", when="ready")]
        )
        assert contributions.override("main") is None
        assert contributions.hold("a.ext", {"ready": True})
        held = contributions.override("main")
        assert held is not None and held[1].text == "ours"
        assert not contributions.hold("a.ext", {"ready": True}), "nothing moved"

    def test_a_condition_said_before_the_contributions_arrive_is_kept(self):
        """A process that starts quickly says what holds before igni has
        fetched its text."""
        contributions = PromptContributions()
        contributions.hold("a.ext", {"ready": True})
        contributions.set_owner(
            "a.ext", "Ay", overrides=[PromptOverride(target="main", text="ours", when="ready")]
        )
        assert contributions.override("main") is not None

    def test_every_change_is_announced(self):
        contributions = PromptContributions()
        seen: list[int] = []
        contributions.watch(lambda: seen.append(1))
        contributions.set_owner("a.ext", "Ay")
        contributions.hold("a.ext", {"x": True})
        contributions.set_order(["a.ext"])
        contributions.remove_owner("a.ext")
        assert len(seen) == 4

    def test_two_replacements_of_one_section_keep_the_first(self, caplog):
        """The resolver stops this ever arriving; if it does, never a merge."""
        contributions = PromptContributions()
        contributions.set_owner("a.ext", "Ay", edits=[_replace("main", "x", "a")])
        contributions.set_owner("b.ext", "Bee", edits=[_replace("main", "x", "b")])
        base = "<!-- section:x -->\nigni\n<!-- /section:x -->"
        assert _composed(contributions, "main", base) == "a"
        assert "Services page" in caplog.text

    def test_it_knows_which_agents_it_shapes(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            overrides=[PromptOverride(target="agent:explorer", text="x", when="later")],
            edits=[_append("agent:architect", "t", "tail")],
        )
        assert contributions.agents() == ["architect"]


class TestTheMainPrompt:
    def test_with_nothing_installed_it_is_igni_s(self):
        rendered = _render(PromptContributions())
        assert "## Safety" in rendered
        assert "nowhere to file project knowledge" in rendered
        assert "<!-- section" not in rendered and "<!-- /section" not in rendered

    def test_a_replaced_section_replaces_only_that_section(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", edits=[_replace("main", "safety", "## Safety\n\nOur safety rules.")]
        )
        rendered = _render(contributions)
        assert "Our safety rules." in rendered
        assert "Don't write secrets to disk" not in rendered
        assert "## Editing Guidelines" in rendered

    def test_an_override_is_the_whole_prompt(self):
        """Full control: nothing of igni's text, and igni's placeholders
        still work inside the extension's."""
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            overrides=[
                PromptOverride(
                    target="main",
                    text="<!-- section:who -->\nYou are Acme's agent.\n<!-- /section:who -->\n\n"
                    "<!-- section:team -->\n{{AGENT_CATALOG}}\n<!-- /section:team -->",
                )
            ],
        )
        rendered = _render(contributions)
        assert rendered.startswith("You are Acme's agent.")
        assert "researcher: reads things" in rendered
        assert "## Safety" not in rendered

    def test_the_persisting_block_is_the_prompt_s_own_sections(self):
        """``{{PERSISTING}}`` is pulled in before edits land, so its
        sections can be replaced like any other."""
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            edits=[
                _replace("main", "project_documents", "## Our documents"),
                _replace("main", "durable_knowledge", "### File it with us"),
            ],
        )
        rendered = _render(contributions)
        assert "## Our documents" in rendered and "### File it with us" in rendered
        assert "nowhere to file project knowledge" not in rendered
        assert "When you learn something durable about the project" not in rendered

    def test_an_added_section_may_use_the_conditionals(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            edits=[
                _append(
                    "main",
                    "x",
                    "<!-- user-memory:only -->with memory<!-- /user-memory:only -->"
                    "<!-- user-memory:absent -->without<!-- /user-memory:absent -->",
                )
            ],
        )
        assert _render(contributions).endswith("with memory")
        assert _render(contributions, user_memory=False).endswith("without")


class TestPlanMode:
    def test_with_nothing_installed_it_is_igni_s(self):
        rendered = PlanModeNudge(contributions=PromptContributions()).render()
        assert rendered.startswith("PLAN MODE")
        assert "<!--" not in rendered

    def test_its_sections_are_reachable(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            edits=[
                _append("plan_mode", "index", "Ask the index."),
                _replace("plan_mode", "skip", "Never skip plan mode."),
            ],
        )
        rendered = PlanModeNudge(contributions=contributions).render()
        assert rendered.endswith("Ask the index.")
        assert "Never skip plan mode." in rendered
        assert "Skip plan mode for simple" not in rendered

    def test_the_main_prompt_s_edits_do_not_reach_it(self):
        contributions = PromptContributions()
        contributions.set_owner("a.ext", "Ay", edits=[_append("main", "x", "main only")])
        assert "main only" not in PlanModeNudge(contributions=contributions).render()
