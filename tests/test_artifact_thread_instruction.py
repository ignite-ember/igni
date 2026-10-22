"""Asking the agent to do something in a thread other people can see.

The local comment rail answers by appending a version — one person,
their own document, and the reply *is* the new text. A shared thread is
a different room, so here the agent proposes and a person presses
Accept.

The parse is the load-bearing part, because what it returns is handed
to a button that **writes**. Everything below is about it refusing to
produce a suggestion it was not clearly given.
"""

from __future__ import annotations

from ember_code.core.artifacts.threads import (
    MAX_REPLY_CHARS,
    ThreadInstruction,
)

DOC = "# Auth model\n\nAuth is a signed JWT.\n\nRevocation is checked on refresh only."


def instruction(**over) -> ThreadInstruction:
    base = dict(
        document_name="Auth model",
        document=DOC,
        quote="Auth is a signed JWT.",
        thread=[
            ("dana@acme.test", "Worth naming the cookie and its lifetime."),
            ("maya@acme.test", "Agreed."),
        ],
        instruction="@igni add the cookie name and lifetime",
    )
    base.update(over)
    return ThreadInstruction(**base)


class TestReadingTheAnswer:
    def test_a_plain_reply_with_no_suggestion(self):
        answer = ThreadInstruction.parse("The cookie is named igni_rt and lasts 14 days.")
        assert answer.body.startswith("The cookie is named")
        assert answer.suggestion == ""

    def test_a_reply_that_proposes_a_document(self):
        answer = ThreadInstruction.parse(
            "Added the name and the lifetime.\n\n"
            "SUGGESTION:\n"
            "# Auth model\n\nAuth is a signed JWT in `igni_rt`, good for 14 days."
        )
        assert answer.body == "Added the name and the lifetime."
        assert answer.suggestion.startswith("# Auth model")
        # The marker is not part of either half.
        assert "SUGGESTION" not in answer.body
        assert "SUGGESTION" not in answer.suggestion

    def test_no_marker_means_no_suggestion(self):
        # Not a partial document. What this returns is handed to a
        # button that writes.
        answer = ThreadInstruction.parse("I think the whole section should be rewritten.")
        assert answer.suggestion == ""
        assert answer.body

    def test_a_marker_followed_by_an_excuse_is_not_a_document(self):
        # The model sometimes writes the marker and then explains
        # itself. Twelve characters is not a document, and handing it
        # to Accept would replace somebody's page with an apology.
        answer = ThreadInstruction.parse("Here you go.\n\nSUGGESTION:\nsee above")
        assert answer.suggestion == ""
        assert "see above" in answer.body

    def test_an_empty_answer(self):
        answer = ThreadInstruction.parse("")
        assert answer.body == ""
        assert answer.suggestion == ""

    def test_a_long_reply_is_cut_rather_than_shown_whole(self):
        answer = ThreadInstruction.parse("word " * 800)
        assert len(answer.body) <= MAX_REPLY_CHARS + 1
        assert answer.body.endswith("…")

    def test_the_suggested_document_is_never_cut(self):
        # Clipping the reply is cosmetic; clipping a document would
        # truncate what Accept writes.
        long_doc = "# Auth model\n\n" + ("paragraph. " * 900)
        answer = ThreadInstruction.parse(f"Rewritten.\n\nSUGGESTION:\n{long_doc}")
        assert answer.suggestion.strip().endswith("paragraph.")
        assert len(answer.suggestion) > MAX_REPLY_CHARS


class TestWhatTheAgentIsTold:
    def test_the_whole_thread_is_given(self):
        # A reply is rarely self-contained: "yes, do that" means
        # nothing without the two messages above it.
        prompt = instruction().prompt_for()
        assert "Worth naming the cookie" in prompt
        assert "dana@acme.test" in prompt

    def test_the_passage_under_discussion(self):
        assert "Auth is a signed JWT." in instruction().prompt_for()

    def test_the_document_as_it_stands(self):
        assert "Revocation is checked on refresh only." in instruction().prompt_for()

    def test_it_says_people_are_reading(self):
        assert "people who can see this thread" in instruction().prompt_for()

    def test_it_says_nothing_is_applied_automatically(self):
        # The reason the agent need not hedge, and the reason it must
        # not rewrite freely.
        assert "Nothing you write is applied automatically" in instruction().prompt_for()

    def test_it_prefers_a_question_to_a_guess(self):
        assert "A guess that rewrites" in instruction().prompt_for()

    def test_it_asks_for_the_whole_document_not_a_fragment(self):
        assert "the complete " in instruction().prompt_for()

    def test_a_long_document_is_clipped_not_dropped(self):
        prompt = instruction(document="x" * 60_000).prompt_for()
        assert "Auth model" in prompt
        assert len(prompt) < 40_000

    def test_a_long_thread_keeps_the_recent_end(self):
        # The end is where the instruction's context is.
        thread = [(f"p{i}@acme.test", f"message {i}") for i in range(60)]
        prompt = instruction(thread=thread).prompt_for()
        assert "message 59" in prompt
        assert "message 0:" not in prompt
