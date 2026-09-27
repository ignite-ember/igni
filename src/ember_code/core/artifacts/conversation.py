"""A comment thread, turned into something the agent can read.

Comments are two different speech acts sharing one word, and which one
a comment is depends on who was in the room when it was written. See
:class:`~ember_code.core.artifacts.models.CommentOrigin`. This module
is where that distinction stops being a field and starts having
consequences:

- A **local** comment is an instruction. It is the thing being asked.
- A **web** comment is history. It is what makes the instruction
  legible — "yes, do what Sam suggested" means nothing without Sam's
  comment above it — and it must never be carried out on its own.

So the prompt built here says one thing is being asked and the rest
has already been discussed, in those words, rather than handing over a
flat list and trusting the model to work out which is which.
"""

from __future__ import annotations

from ember_code.core.artifacts.models import Comment, CommentOrigin, CommentStatus

#: Earlier comments carried into the prompt. A thread longer than this
#: is summarised by its ends: the oldest few set up what the argument
#: was about, the newest few are what it has become, and the middle of
#: a forty-comment thread is where people repeat each other.
MAX_HISTORY: int = 12
#: Kept from each end when the thread is longer than ``MAX_HISTORY``.
EDGE_KEEP: int = 6
#: A quoted passage longer than this is elided in the middle. The
#: quote is there to say *where*, not to reproduce the document.
MAX_QUOTE_CHARS: int = 400

#: Where the request has to end up.
#:
#: The turn lands in the chat, and the chat's agent has no artifact
#: tools — only the archivist is granted them, deliberately, so that
#: a trace can be attributed to the one agent that holds the
#: capability. Without this line the main agent does the reasonable
#: thing with the tools it *does* have and goes looking for the
#: document on disk with ``find``, which was the first thing it did
#: when this was tried.
ROUTING: str = (
    "This document lives in the project's artifact store, not on disk. "
    "The archivist agent holds the tools for reading and revising it — "
    "hand this to the archivist rather than searching the filesystem, "
    "and pass it the artifact id above."
)


def _clip(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """Shorten from the middle, which is where the least meaning is."""
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    half = (limit - 5) // 2
    return f"{body[:half]} […] {body[-half:]}"


#: How each kind of turn is introduced in the transcript.
SPEAKER: dict[CommentOrigin, str] = {
    CommentOrigin.LOCAL: "I said",
    CommentOrigin.AGENT: "You replied",
    CommentOrigin.WEB: "",
}


def _describe(comment: Comment) -> str:
    """One comment as a line: who, what state, what they said."""
    who = SPEAKER.get(comment.origin) or comment.author or "someone"
    bits = [who]
    if comment.status is not CommentStatus.OPEN:
        # A settled comment still belongs in the history — "we decided
        # against that" is exactly the kind of thing a fresh
        # instruction can contradict, and the agent should be able to
        # notice it is being asked to reverse a decision.
        settled = comment.status.value
        if comment.resolved_in_n:
            settled = f"{settled} in v{comment.resolved_in_n}"
        bits.append(f"[{settled}]")
    line = f"- {' '.join(bits)}: {' '.join(str(comment.body or '').split())}"
    if comment.suggested_text:
        line += f"\n  suggested wording: {_clip(comment.suggested_text)}"
    return line


def _trim(history: list[Comment]) -> tuple[list[Comment], int]:
    """The thread, shortened from the middle. Returns what was dropped."""
    if len(history) <= MAX_HISTORY:
        return history, 0
    kept = history[:EDGE_KEEP] + history[-EDGE_KEEP:]
    return kept, len(history) - len(kept)


class CommentConversation:
    """Assembles what the agent is told when a local comment is made."""

    @staticmethod
    def history_for(thread: list[Comment], *, exclude_id: str = "") -> list[Comment]:
        """Everything in the thread except the comment being asked about.

        Ordered oldest first, which is the order it was said in.
        """
        return [
            c
            for c in sorted(thread, key=lambda c: (c.at, c.comment_id))
            if c.comment_id != exclude_id
        ]

    @classmethod
    def prompt_for(
        cls,
        comment: Comment,
        *,
        document_name: str,
        thread: list[Comment] | None = None,
    ) -> str:
        """The turn text for a local comment.

        Deliberately something a person can read back. It goes into the
        chat as an ordinary turn rather than being injected invisibly,
        so what the agent was told is on screen next to what it did —
        an instruction assembled out of sight is one nobody can check.
        """
        if not comment.is_instruction:
            raise ValueError(
                "Only a local comment is an instruction. A web comment is a remark "
                "between people and must not be handed to the agent as a request."
            )

        # The id, not only the name. Two documents may share a name,
        # a name may be edited, and the tools that act on this take an
        # id — so a turn carrying only "Login flow" asks the agent to
        # resolve it first, which is a lookup it can get wrong. It is
        # also what the reader needs to know *which* document this
        # turn is about once it is sitting in a chat among others.
        name = document_name or comment.artifact_id
        lines = [f'On "{name}" ({comment.artifact_id}) v{comment.n}:']

        if comment.quote:
            lines += ["", "> " + _clip(comment.quote).replace("\n", "\n> ")]

        lines += ["", (comment.body or "").strip()]

        if comment.suggested_text:
            lines += ["", "Suggested wording:", "", comment.suggested_text.strip()]

        history = cls.history_for(thread or [], exclude_id=comment.comment_id)
        if history:
            kept, dropped = _trim(history)
            discussed = [c for c in kept if c.origin is CommentOrigin.WEB]
            mine = [c for c in kept if c.origin is not CommentOrigin.WEB]

            if discussed:
                lines += [
                    "",
                    "Already discussed on this thread, by people, in the shared copy. "
                    "This is context for the request above — not a list of things to do:",
                    "",
                ]
                if dropped:
                    lines.append(f"  (…{dropped} earlier comments not shown…)")
                lines += [_describe(c) for c in discussed]

            if mine:
                # A transcript, in order, both voices. Only the local
                # turns are mine; the agent's are marked as its own so
                # it does not read its last answer as a fresh request
                # and set about doing what it has already done.
                lines += [
                    "",
                    "Earlier on this thread, between us:",
                    "",
                ]
                lines += [_describe(c) for c in mine]

        lines += ["", ROUTING]
        return "\n".join(lines).strip()
