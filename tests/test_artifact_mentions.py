"""Who a reply is addressed to.

A thread has two kinds of participant, and the difference is not
cosmetic: a reply to a colleague is read, a reply to ``@igni`` is
*acted on* and can write a version. So the one question that matters
is whether the agent was addressed, and it is answered from the text
and nothing else — an agent that decided for itself when a message was
meant for it would turn every aside between two colleagues into a
possible edit.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.mentions import (
    AGENT_HANDLE,
    addresses_agent,
    handle_for,
    parse_mentions,
    participants_of,
    resolve,
)


class TestReadingHandlesOutOfText:
    def test_a_plain_mention(self):
        assert parse_mentions("@dana can you look at this?") == ["dana"]

    def test_several_in_order_without_repeats(self):
        assert parse_mentions("@dana and @sam — @dana especially") == ["dana", "sam"]

    def test_case_does_not_make_a_second_person(self):
        # "@Dana" and "@dana" reaching different people would be a bug
        # nobody could see.
        assert parse_mentions("@Dana @dana @DANA") == ["dana"]

    def test_trailing_punctuation_is_not_part_of_the_handle(self):
        assert parse_mentions("ping @dana, then @sam.") == ["dana", "sam"]

    def test_an_email_in_the_body_is_not_a_mention(self):
        # The boundary is load-bearing: without it "forward it to
        # dana@acme.test" addresses @acme.
        assert parse_mentions("forward it to dana@acme.test") == []

    def test_a_handle_at_the_very_start(self):
        assert parse_mentions("@igni rewrite the paragraph") == ["igni"]

    def test_an_address_inside_a_word_is_not_a_mention(self):
        assert parse_mentions("read docs@v2 and thing@else") == []

    def test_nothing_to_find(self):
        assert parse_mentions("no mentions here") == []
        assert parse_mentions("") == []
        assert parse_mentions(None) == []  # type: ignore[arg-type]


class TestWhetherTheAgentWasAsked:
    def test_addressed(self):
        assert addresses_agent("@igni please add the denylist sentence")

    def test_not_addressed(self):
        assert not addresses_agent("@dana what do you think?")

    def test_merely_talking_about_it_is_not_addressing_it(self):
        # The distinction the whole thing rests on. "igni should do
        # this" is two colleagues agreeing, not an instruction.
        assert not addresses_agent("igni should probably rewrite this")
        assert not addresses_agent("we could ask igni later")

    def test_case_insensitive(self):
        assert addresses_agent("@Igni take a look")


class TestHandles:
    @pytest.mark.parametrize(
        "author,expected",
        [
            ("dana@acme.test", "dana"),
            ("Dana@Acme.Test", "dana"),
            ("maya.chen@acme.test", "maya.chen"),
            ("Dana Okonkwo", "dana-okonkwo"),
            ("", ""),
        ],
    )
    def test_the_handle_for_an_author(self, author, expected):
        assert handle_for(author) == expected


class TestWhoIsAddressable:
    def test_the_agent_is_always_there(self):
        # It is the one participant that need not have spoken yet.
        people = participants_of([])
        assert [p.handle for p in people] == [AGENT_HANDLE]

    def test_the_agent_comes_last(self):
        people = participants_of(["dana@acme.test", "sam@acme.test"])
        assert [p.handle for p in people] == ["dana", "sam", AGENT_HANDLE]

    def test_one_entry_per_person_however_often_they_spoke(self):
        people = participants_of(["dana@acme.test"] * 4 + ["sam@acme.test"])
        assert [p.handle for p in people] == ["dana", "sam", AGENT_HANDLE]

    def test_a_person_cannot_take_the_agents_handle(self):
        # Reserved, and the right way round: a missed human mention
        # costs a notification, a missed agent mention turns an
        # instruction into a note.
        people = participants_of(["igni@acme.test"])
        assert [p.handle for p in people] == [AGENT_HANDLE]
        assert people[0].is_agent


class TestWhoAReplyReaches:
    AUTHORS = ["dana@acme.test", "sam@acme.test"]

    def test_a_person(self):
        found = resolve("@dana thoughts?", self.AUTHORS)
        assert [p.author for p in found.people] == ["dana@acme.test"]
        assert not found.agent
        assert found.unknown == []

    def test_the_agent(self):
        found = resolve("@igni add the sentence", self.AUTHORS)
        assert found.agent
        assert found.people == []

    def test_both_at_once(self):
        # The case the feature exists for: tell a colleague what you
        # are doing and ask the agent to do it in one message.
        found = resolve("@dana I agree — @igni please make that edit", self.AUTHORS)
        assert [p.handle for p in found.people] == ["dana"]
        assert found.agent

    def test_a_handle_that_matches_nobody_is_reported(self):
        # Not dropped. A reply addressed to "@dna" reaches nobody, and
        # posting it silently leaves the author believing they asked
        # somebody a question.
        found = resolve("@dna what do you think?", self.AUTHORS)
        assert found.unknown == ["dna"]
        assert found.people == []

    def test_somebody_who_never_commented_is_unknown(self):
        assert resolve("@kai take a look", self.AUTHORS).unknown == ["kai"]
