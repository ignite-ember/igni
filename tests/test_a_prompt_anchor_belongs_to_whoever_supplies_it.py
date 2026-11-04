"""Prompt anchors: filled by their owner, left alone by everyone else.

``{{CODEINDEX_GRAPH_SCHEMA}}`` was substituted by a function that
imported the schema module directly. That import is the coupling the
extension split has to remove — the base cannot import from a
capability that may not be installed — so the substitution became a
table of named anchors with an owner each.

The rule that matters is the one about anchors this table does *not*
own. ``load_prompt`` is not the last pass over a prompt:
``{{PERSISTING}}``, ``{{AGENT_CATALOG}}`` and ``{{CODEINDEX_TOOLS}}``
are filled by the prompt builder afterwards. The first version of
``expand`` dropped every anchor it had no supplier for and deleted
three sections of the system prompt. Hence the second class below.
"""

from __future__ import annotations

import pytest

from ember_code.core.prompts.fragments import PromptFragments


@pytest.fixture
def fragments() -> PromptFragments:
    return PromptFragments()


class TestFillingWhatItOwns:
    def test_an_anchor_becomes_what_its_supplier_returns(self, fragments):
        fragments.register("THING", "acme.ext", lambda: "a described thing")
        assert fragments.expand("before {{THING}} after") == "before a described thing after"

    def test_the_supplier_is_not_called_until_expansion(self, fragments):
        calls: list[int] = []
        fragments.register("THING", "acme.ext", lambda: calls.append(1) or "x")

        assert calls == []
        fragments.expand("{{THING}}")
        assert calls == [1]

    def test_every_occurrence_is_filled(self, fragments):
        fragments.register("THING", "acme.ext", lambda: "x")
        assert fragments.expand("{{THING}} {{THING}}") == "x x"

    def test_text_with_no_anchors_comes_back_identical(self, fragments):
        assert fragments.expand("plain prompt") == "plain prompt"


class TestLeavingWhatItDoesNot:
    def test_an_anchor_with_no_supplier_survives_untouched(self, fragments):
        # The bug this is here for: ``load_prompt`` is one pass of
        # several, and the next one fills these.
        text = "a {{PERSISTING}} b {{AGENT_CATALOG}} c"
        assert fragments.expand(text) == text

    def test_a_known_anchor_is_filled_without_disturbing_the_others(self, fragments):
        fragments.register("MINE", "acme.ext", lambda: "filled")
        assert fragments.expand("{{MINE}} and {{THEIRS}}") == "filled and {{THEIRS}}"

    def test_an_anchor_whose_owner_left_goes_back_to_being_untouched(self, fragments):
        fragments.register("MINE", "acme.ext", lambda: "filled")
        fragments.remove_owner("acme.ext")
        assert fragments.expand("{{MINE}}") == "{{MINE}}"


class TestWhenASupplierFails:
    def test_the_anchor_is_removed_rather_than_shown_to_a_model(self, fragments):
        def boom() -> str:
            raise RuntimeError("the index is not up")

        fragments.register("THING", "acme.ext", boom)

        # Removed, not left: this one *is* owned, so nothing else is
        # coming to fill it, and braces in front of a model are worse
        # than silence.
        assert fragments.expand("before {{THING}} after") == "before  after"

    def test_one_failure_does_not_take_the_other_anchors_with_it(self, fragments):
        fragments.register("GOOD", "a.ext", lambda: "ok")
        fragments.register("BAD", "b.ext", lambda: (_ for _ in ()).throw(RuntimeError("no")))

        assert fragments.expand("{{GOOD}} {{BAD}} {{UNOWNED}}") == "ok  {{UNOWNED}}"

    def test_the_failure_is_logged_with_the_owner(self, fragments, caplog):
        fragments.register("THING", "acme.ext", lambda: (_ for _ in ()).throw(RuntimeError("no")))

        with caplog.at_level("WARNING"):
            fragments.expand("{{THING}}")

        assert "THING" in caplog.text
        assert "acme.ext" in caplog.text


class TestWhoMayClaimAnAnchor:
    def test_two_extensions_cannot_supply_the_same_anchor(self, fragments):
        assert fragments.register("THING", "first.ext", lambda: "a") is None

        refused = fragments.register("THING", "second.ext", lambda: "b")

        assert refused is not None
        assert "first.ext" in refused
        assert fragments.expand("{{THING}}") == "a"

    def test_the_same_owner_may_replace_its_own_supplier(self, fragments):
        fragments.register("THING", "acme.ext", lambda: "old")

        # What an upgrade does.
        assert fragments.register("THING", "acme.ext", lambda: "new") is None
        assert fragments.expand("{{THING}}") == "new"

    def test_a_name_that_is_not_an_anchor_is_refused(self, fragments):
        for bad in ("lower", "1LEADING", "has space", "", "HAS-DASH"):
            assert fragments.register(bad, "acme.ext", lambda: "x") is not None

    def test_leaving_frees_the_name(self, fragments):
        fragments.register("THING", "first.ext", lambda: "a")
        fragments.remove_owner("first.ext")

        assert fragments.register("THING", "second.ext", lambda: "b") is None
        assert fragments.expand("{{THING}}") == "b"


def test_igni_supplies_no_anchors_of_its_own():
    """The table starts empty: every anchor is an installed extension's.
    ``{{CODEINDEX_GRAPH_SCHEMA}}`` used to be filled by igni importing
    the schema; CodeIndex supplies it now, from its ``graph_schema`` tool."""
    from ember_code.core.prompts.fragments import PromptFragments

    assert PromptFragments().anchors() == []
