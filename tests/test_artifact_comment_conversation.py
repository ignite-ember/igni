"""Local comments are instructions; web comments are history.

The whole comment model turns on one distinction, and this is where it
is pinned. A thread pulled down from the shared copy is what makes a
local comment legible — "yes, do what Sam suggested" means nothing
without Sam's comment above it — but replaying other people's remarks
as a backlog of commands is the failure mode, and the one that would
be discovered by the agent silently rewriting a document nobody asked
it to touch.
"""

from __future__ import annotations

import pytest

from ember_code.core.artifacts.conversation import (
    EDGE_KEEP,
    MAX_HISTORY,
    CommentConversation,
)
from ember_code.core.artifacts.models import Comment, CommentOrigin, CommentStatus


def comment(**kw) -> Comment:
    base = {
        "comment_id": "c1",
        "artifact_id": "a1",
        "n": 2,
        "thread_id": "t1",
        "body": "Should this be configurable?",
        "author": "maya@acme.test",
        "at": "2026-09-27T10:00:00Z",
        "origin": CommentOrigin.LOCAL,
    }
    return Comment(**{**base, **kw})


def web(**kw) -> Comment:
    return comment(origin=CommentOrigin.WEB, **kw)


class TestWhoACommentIsAddressedTo:
    def test_a_local_comment_is_an_instruction(self):
        assert comment().is_instruction is True

    def test_a_web_comment_is_not(self):
        assert web().is_instruction is False

    def test_a_comment_with_no_origin_recorded_is_treated_as_web(self):
        # Every comment predating the field arrived from the server.
        # The narrower reading is also the safer one: history that
        # goes unacted on is a smaller failure than a remark carried
        # out by itself.
        assert Comment(comment_id="x", artifact_id="a", n=1).origin is CommentOrigin.WEB

    def test_a_web_comment_cannot_be_turned_into_a_request(self):
        with pytest.raises(ValueError, match="remark between people"):
            CommentConversation.prompt_for(web(), document_name="Auth")


class TestTheInstruction:
    def test_it_names_the_document_and_version(self):
        text = CommentConversation.prompt_for(comment(), document_name="Auth model")
        assert 'On "Auth model" (a1) v2:' in text

    def test_it_carries_the_artifact_id_not_only_the_name(self):
        # The tools that act on this take an id. A turn carrying only
        # a name asks the agent to resolve it first, which is a lookup
        # it can get wrong — and names are neither unique nor fixed.
        text = CommentConversation.prompt_for(
            comment(artifact_id="7ea8d39931b94b48"), document_name="Auth model"
        )
        assert "7ea8d39931b94b48" in text

    def test_it_carries_the_passage_as_a_quote(self):
        text = CommentConversation.prompt_for(
            comment(quote="It expires after 15 minutes"), document_name="Auth"
        )
        assert "> It expires after 15 minutes" in text

    def test_it_carries_the_comment_itself(self):
        text = CommentConversation.prompt_for(comment(), document_name="Auth")
        assert "Should this be configurable?" in text

    def test_a_comment_about_the_whole_document_quotes_nothing(self):
        # The other surface: the composer under the article, which is
        # for the remark that has no particular sentence attached. A
        # quote block invented for it would tell the agent a passage
        # was meant when none was.
        text = CommentConversation.prompt_for(comment(quote=""), document_name="Auth")
        assert ">" not in text.split("archivist")[0]
        assert "Should this be configurable?" in text

    def test_a_long_quote_is_elided_in_the_middle(self):
        # The quote says *where*, and is not there to reproduce the
        # document — the agent can read that for itself.
        text = CommentConversation.prompt_for(comment(quote="x" * 900), document_name="Auth")
        assert "[…]" in text
        assert len(text) < 900


class TestTheThreadAsContext:
    def test_earlier_web_comments_come_through_as_discussion(self):
        thread = [
            web(comment_id="w1", author="sam@acme.test", body="15m feels short", at="..1"),
            comment(comment_id="c1", at="..2"),
        ]
        text = CommentConversation.prompt_for(thread[1], document_name="Auth", thread=thread)
        assert "sam@acme.test: 15m feels short" in text

    def test_they_are_labelled_as_context_and_not_as_work(self):
        # The sentence doing the load-bearing work. Without it the
        # agent gets a flat list and has to infer which half it was
        # asked to act on.
        thread = [web(comment_id="w1", body="15m feels short", at="..1"), comment(at="..2")]
        text = CommentConversation.prompt_for(thread[1], document_name="Auth", thread=thread)
        assert "not a list of things to do" in text

    def test_the_comment_being_asked_about_is_not_repeated_as_history(self):
        thread = [comment(at="..1")]
        text = CommentConversation.prompt_for(thread[0], document_name="Auth", thread=thread)
        assert text.count("Should this be configurable?") == 1

    def test_my_own_earlier_comments_are_kept_apart_from_other_people_s(self):
        # They are different things: one is what was decided with
        # colleagues, the other is what I already asked for.
        thread = [
            web(comment_id="w1", author="sam@acme.test", body="Sam's point", at="..1"),
            comment(comment_id="m1", body="My earlier ask", at="..2"),
            comment(comment_id="c1", at="..3"),
        ]
        text = CommentConversation.prompt_for(thread[2], document_name="Auth", thread=thread)
        # The heading names the thread as an exchange between us, now
        # that the agent's own turns are recorded in it too.
        assert "Earlier on this thread, between us:" in text
        assert text.index("Sam's point") < text.index("Earlier on this thread")

    def test_a_settled_comment_says_so(self):
        # "We decided against that" is exactly what a fresh
        # instruction can contradict, and the agent should be able to
        # see it is being asked to reverse a decision.
        thread = [
            web(
                comment_id="w1",
                body="Make it 30m",
                status=CommentStatus.REJECTED,
                at="..1",
            ),
            comment(at="..2"),
        ]
        text = CommentConversation.prompt_for(thread[1], document_name="Auth", thread=thread)
        assert "[rejected]" in text

    def test_an_accepted_comment_names_the_version_that_applied_it(self):
        thread = [
            web(
                comment_id="w1",
                body="Make it 30m",
                status=CommentStatus.ACCEPTED,
                resolved_in_n=3,
                at="..1",
            ),
            comment(at="..2"),
        ]
        text = CommentConversation.prompt_for(thread[1], document_name="Auth", thread=thread)
        assert "[accepted in v3]" in text

    def test_history_is_ordered_oldest_first(self):
        thread = [
            web(comment_id="w2", body="second", at="..2"),
            web(comment_id="w1", body="first", at="..1"),
            comment(at="..3"),
        ]
        text = CommentConversation.prompt_for(thread[2], document_name="Auth", thread=thread)
        assert text.index("first") < text.index("second")

    def test_a_long_thread_keeps_both_ends_and_says_what_it_dropped(self):
        # The middle of a forty-comment thread is where people repeat
        # each other. Dropping it silently would let the agent believe
        # it had seen the whole argument.
        thread = [
            web(comment_id=f"w{i}", body=f"remark {i}", at=f"..{i:03d}")
            for i in range(MAX_HISTORY + 10)
        ]
        mine = comment(at="..999")
        thread.append(mine)

        text = CommentConversation.prompt_for(mine, document_name="Auth", thread=thread)

        assert "remark 0" in text
        assert f"remark {MAX_HISTORY + 9}" in text
        assert "earlier comments not shown" in text
        assert text.count("remark ") == EDGE_KEEP * 2

    def test_no_history_section_when_there_is_no_history(self):
        text = CommentConversation.prompt_for(comment(), document_name="Auth", thread=[])
        assert "Already discussed" not in text


class TestATwoSidedThread:
    """The rail's replies, recorded so the thread survives a restart.

    Continuation used to work only because every comment landed in
    the same chat session — the agent remembered, the thread did not.
    A transcript that lives with the document does not depend on a
    process staying up.
    """

    def agent(self, **kw) -> Comment:
        return comment(origin=CommentOrigin.AGENT, author="igni", **kw)

    def test_the_agent_s_own_reply_is_never_an_instruction(self):
        # Handed its last answer as a request, an agent sets about
        # doing what it has already done.
        assert self.agent(body="Done, now v4.").is_instruction is False

    def test_a_reply_cannot_be_turned_into_a_request(self):
        with pytest.raises(ValueError, match="remark between people|Only a local"):
            CommentConversation.prompt_for(self.agent(), document_name="Auth")

    def test_both_voices_reach_the_prompt_in_order(self):
        thread = [
            comment(comment_id="m1", body="Name the TTL.", at="..1"),
            self.agent(comment_id="a1", body="Which unit — seconds or minutes?", at="..2"),
            comment(comment_id="m2", body="Minutes.", at="..3"),
        ]
        text = CommentConversation.prompt_for(thread[2], document_name="Auth", thread=thread)

        assert "I said: Name the TTL." in text
        assert "You replied: Which unit" in text
        assert text.index("Name the TTL.") < text.index("Which unit")

    def test_each_side_is_marked_as_whose_it_was(self):
        # A flat list leaves the agent to infer which half it wrote,
        # and the half it wrote is the half it must not redo.
        thread = [
            comment(comment_id="m1", body="First ask.", at="..1"),
            self.agent(comment_id="a1", body="First answer.", at="..2"),
            comment(comment_id="m2", body="Second ask.", at="..3"),
        ]
        text = CommentConversation.prompt_for(thread[2], document_name="Auth", thread=thread)
        assert "I said: First ask." in text
        assert "You replied: First answer." in text

    def test_a_pulled_down_web_thread_stays_separate_from_ours(self):
        # Three voices now: colleagues on the shared copy, me, and the
        # agent. Only the first must never be carried out.
        thread = [
            web(comment_id="w1", author="sam@acme.test", body="Sam's point", at="..1"),
            self.agent(comment_id="a1", body="Earlier answer", at="..2"),
            comment(comment_id="m1", at="..3"),
        ]
        text = CommentConversation.prompt_for(thread[2], document_name="Auth", thread=thread)

        assert "not a list of things to do" in text
        assert "Sam's point" in text
        assert "You replied: Earlier answer" in text
        assert text.index("Sam's point") < text.index("Earlier answer")


class TestRouting:
    def test_it_says_where_the_document_actually_lives(self):
        # The chat's agent holds no artifact tools — only the
        # archivist does. Driving this for real, the main agent did
        # the reasonable thing with what it had and ran ``find`` over
        # the project looking for the document as a file.
        text = CommentConversation.prompt_for(comment(), document_name="Auth")
        assert "artifact store" in text
        assert "archivist" in text
        assert "rather than searching the filesystem" in text

    def test_it_comes_last_so_the_request_is_read_first(self):
        text = CommentConversation.prompt_for(comment(), document_name="Auth")
        assert text.index("Should this be configurable?") < text.index("archivist")

    def test_the_old_no_history_case_still_holds(self):
        text = CommentConversation.prompt_for(comment(), document_name="Auth", thread=[])
        assert "Already discussed" not in text
