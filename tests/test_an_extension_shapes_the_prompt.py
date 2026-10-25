"""An extension may take over any part of the agent's prompt.

The whole of it, one named section, or the text in a slot igni leaves —
the main prompt and plan mode alike. A deployment that wants its agent
to work its own way gets exactly that; what this pins is that each
level does what it says and nothing else, and that igni's own text is
what is left when nothing is installed.

Who wins when two extensions want the same thing is decided before
anything reaches the table here (``test_extensions_collide_and_a_super_admin_decides``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.prompts import load_prompt
from ember_code.core.prompts.contributions import (
    PromptClaim,
    PromptClaimKind,
    PromptContributions,
)
from ember_code.core.prompts.document import (
    MAX_SLOT_CHARS,
    PromptDocument,
    PromptDocumentName,
    PromptTarget,
)
from ember_code.core.session.agent_builder.plan_mode_nudge import PlanModeNudge
from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder

MAIN = PromptDocumentName.MAIN
PLAN = PromptDocumentName.PLAN_MODE


class _Settings:
    class storage:  # noqa: N801
        data_dir = Path("/nonexistent")


def _claim(kind: PromptClaimKind, target: str, text: str, when: str | None = None) -> PromptClaim:
    return PromptClaim(kind=kind, target=target, text=text, when=when)


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


class TestTheDocument:
    TEXT = (
        "intro\n<!-- section:a -->\n## A\nigni's a\n<!-- /section:a -->\n"
        "<!-- slot:s -->\ndefault\n<!-- /slot:s -->\n"
        "<!-- section:b -->\n## B\nigni's b\n<!-- /section:b -->\n"
    )

    def test_it_reads_its_sections_and_slots(self):
        document = PromptDocument(self.TEXT)
        assert document.sections() == ["a", "b"]
        assert document.slots() == ["s"]

    def test_a_replaced_section_is_the_replacement_and_nothing_else(self):
        text = PromptDocument(self.TEXT).replace_sections({"a": "## A\nours"}).fill_slots({})
        assert "ours" in text and "igni's a" not in text
        assert "igni's b" in text

    def test_a_slot_nobody_fills_says_its_default(self):
        assert "default" in PromptDocument(self.TEXT).fill_slots({})

    def test_a_filled_slot_says_the_fill_instead(self):
        text = PromptDocument(self.TEXT).fill_slots({"s": "filled"})
        assert "filled" in text and "default" not in text

    def test_no_marker_survives(self):
        assert "<!--" not in PromptDocument(self.TEXT).fill_slots({"s": "x"})

    def test_a_marker_without_its_partner_is_refused(self):
        """A section whose close went missing would swallow the rest of the
        prompt the moment somebody replaced it."""
        with pytest.raises(ValueError, match="unbalanced"):
            PromptDocument("<!-- section:a -->\nno end")

    @pytest.mark.parametrize("spelled", ["main", "main.workflow", "plan_mode.code_intelligence"])
    def test_a_target_spells_back_as_written(self, spelled):
        assert str(PromptTarget.parse(spelled)) == spelled

    @pytest.mark.parametrize("spelled", ["system", "main.Workflow", "main."])
    def test_a_target_igni_cannot_have_is_refused(self, spelled):
        with pytest.raises(ValueError):
            PromptTarget.parse(spelled)


class TestTheTable:
    def test_fills_from_several_extensions_stack_under_their_names(self):
        contributions = PromptContributions()
        contributions.set_owner("b.ext", "Bee", [_claim(PromptClaimKind.SLOT, "main.s", "bee")])
        contributions.set_owner("a.ext", "Ay", [_claim(PromptClaimKind.SLOT, "main.s", "ay")])

        stacked = contributions.slot_fills(MAIN)["s"]
        # In extension-id order, so it does not depend on who started first.
        assert stacked.index("### From Ay") < stacked.index("### From Bee")

    def test_one_fill_is_just_its_text(self):
        contributions = PromptContributions()
        contributions.set_owner("a.ext", "Ay", [_claim(PromptClaimKind.SLOT, "main.s", "ay")])
        assert contributions.slot_fills(MAIN) == {"s": "ay"}

    def test_a_fill_longer_than_a_slot_holds_is_refused(self):
        contributions = PromptContributions()
        [refusal] = contributions.set_owner(
            "a.ext", "Ay", [_claim(PromptClaimKind.SLOT, "main.s", "x" * (MAX_SLOT_CHARS + 1))]
        )
        assert "replace a section" in refusal
        assert contributions.slot_fills(MAIN) == {}

    def test_a_condition_holds_only_once_it_is_said(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", [_claim(PromptClaimKind.REPLACE, "main", "ours", when="ready")]
        )
        assert contributions.replacement(MAIN) is None
        assert contributions.hold("a.ext", {"ready": True})
        assert contributions.replacement(MAIN) == "ours"
        assert not contributions.hold("a.ext", {"ready": True}), "nothing moved"

    def test_a_condition_said_before_the_claims_arrive_is_kept(self):
        """A process that starts quickly says what holds before igni has
        fetched its text."""
        contributions = PromptContributions()
        contributions.hold("a.ext", {"ready": True})
        contributions.set_owner(
            "a.ext", "Ay", [_claim(PromptClaimKind.REPLACE, "main", "ours", when="ready")]
        )
        assert contributions.replacement(MAIN) == "ours"

    def test_every_change_is_announced(self):
        contributions = PromptContributions()
        seen: list[int] = []
        contributions.watch(lambda: seen.append(1))
        contributions.set_owner("a.ext", "Ay", [])
        contributions.hold("a.ext", {"x": True})
        contributions.remove_owner("a.ext")
        assert len(seen) == 3

    def test_two_claims_on_one_section_keep_the_first(self, caplog):
        """The resolver stops this ever arriving; if it does, never a merge."""
        contributions = PromptContributions()
        contributions.set_owner("a.ext", "Ay", [_claim(PromptClaimKind.REPLACE, "main.x", "a")])
        contributions.set_owner("b.ext", "Bee", [_claim(PromptClaimKind.REPLACE, "main.x", "b")])
        assert contributions.section_replacements(MAIN) == {"x": "a"}
        assert "Services page" in caplog.text


class TestTheMainPrompt:
    def test_with_nothing_installed_it_is_igni_s(self):
        rendered = _render(PromptContributions())
        assert "## Safety" in rendered
        assert "nowhere to file project knowledge" in rendered
        assert "<!--" not in rendered.replace("<!-- This file gives", "")

    def test_a_replaced_section_replaces_only_that_section(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            [_claim(PromptClaimKind.REPLACE, "main.safety", "## Safety\n\nOur safety rules.")],
        )
        rendered = _render(contributions)
        assert "Our safety rules." in rendered
        assert "Don't write secrets to disk" not in rendered
        assert "## Editing Guidelines" in rendered

    def test_a_whole_replacement_is_the_whole_prompt(self):
        """Full control: nothing of igni's text, and igni's placeholders and
        slots still work inside the extension's."""
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            [
                _claim(
                    PromptClaimKind.REPLACE,
                    "main",
                    "You are Acme's agent.\n\n{{AGENT_CATALOG}}\n\n"
                    "<!-- slot:code_intelligence -->\n<!-- /slot:code_intelligence -->",
                ),
                _claim(PromptClaimKind.SLOT, "main.code_intelligence", "The index is here."),
            ],
        )
        rendered = _render(contributions)
        assert rendered.startswith("You are Acme's agent.")
        assert "researcher: reads things" in rendered
        assert "The index is here." in rendered
        assert "## Safety" not in rendered

    def test_a_filled_slot_takes_its_default_s_place(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            [_claim(PromptClaimKind.SLOT, "main.project_documents", "## Our documents")],
        )
        rendered = _render(contributions)
        assert "## Our documents" in rendered
        assert "nowhere to file project knowledge" not in rendered

    def test_somewhere_to_persist_to_is_a_filled_slot(self):
        """``can-persist`` without user memory: only when an extension says
        when to file something."""
        nothing = _render(PromptContributions(), user_memory=False)
        assert "When NOT to persist" not in nothing

        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", [_claim(PromptClaimKind.SLOT, "main.when_to_persist", "- file it")]
        )
        filed = _render(contributions, user_memory=False)
        assert "When NOT to persist" in filed
        assert "When you learn something durable about the project" not in filed


class TestPlanMode:
    def test_with_nothing_installed_it_is_igni_s(self):
        rendered = PlanModeNudge(contributions=PromptContributions()).render()
        assert rendered.startswith("PLAN MODE")
        assert "<!--" not in rendered

    def test_its_slot_and_its_sections_are_reachable(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext",
            "Ay",
            [
                _claim(PromptClaimKind.SLOT, "plan_mode.code_intelligence", "Ask the index."),
                _claim(PromptClaimKind.REPLACE, "plan_mode.skip", "Never skip plan mode."),
            ],
        )
        rendered = PlanModeNudge(contributions=contributions).render()
        assert rendered.endswith("Ask the index.")
        assert "Never skip plan mode." in rendered
        assert "Skip plan mode for simple" not in rendered

    def test_the_main_prompt_s_claims_do_not_reach_it(self):
        contributions = PromptContributions()
        contributions.set_owner(
            "a.ext", "Ay", [_claim(PromptClaimKind.SLOT, "main.code_intelligence", "main only")]
        )
        assert "main only" not in PlanModeNudge(contributions=contributions).render()
