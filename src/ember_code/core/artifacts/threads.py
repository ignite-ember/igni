"""Asking the agent to do something, in a thread other people can see.

The local comment rail answers a comment by appending a version. That
is right there: one person, their own document, and the reply *is* the
new text. A shared thread is a different room. Other people are
watching it, the document is theirs as much as anyone's, and an agent
that rewrote it because somebody typed its name would be writing on
their behalf in public.

So here the agent **proposes**. Its answer arrives as a reply like any
other, and any replacement wording it offers arrives as
``suggested_text`` — which the review queue's Accept button already
knows how to apply, to head, with the version it writes named on the
button. Nothing is written until a person presses it.

The thread is given in full because a reply is rarely self-contained.
"yes, do that" means nothing without the two messages above it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Messages of a thread carried into the prompt. A thread longer than
#: this has usually stopped being one conversation.
MAX_MESSAGES: int = 20

#: Characters kept from any one message.
MAX_MESSAGE_CHARS: int = 1_200

#: Characters of the document carried in. Generous: the agent has no
#: tools, so what it is handed is all it can read, and an answer about
#: a document it half saw is worse than no answer.
MAX_DOCUMENT_CHARS: int = 10_000

#: How long an answer may be before it stops being read in a thread.
MAX_REPLY_CHARS: int = 1_500

_SUGGESTION = re.compile(
    r"^\s*SUGGESTION\s*:\s*$(?P<body>.*)", re.IGNORECASE | re.MULTILINE | re.DOTALL
)


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


@dataclass
class ThreadAnswer:
    """What the agent said, and what it proposes changing."""

    body: str = ""
    #: Replacement wording for the whole document, or empty.
    suggestion: str = ""


@dataclass
class ThreadInstruction:
    """Everything the agent is told before it answers a thread."""

    document_name: str
    document: str
    quote: str
    #: ``(author, body)`` oldest first.
    thread: list[tuple[str, str]] = field(default_factory=list)
    instruction: str = ""

    def prompt_for(self) -> str:
        lines = [
            f'You have been asked to help in a comment thread on "{self.document_name}".',
            "",
            "You are replying to people who can see this thread. Answer them, in "
            "their terms, in three sentences at most.",
        ]

        if self.quote.strip():
            lines += ["", "The thread is about this passage:", "", f"  {_squash(self.quote)}"]

        if self.thread:
            lines += ["", "The conversation so far, oldest first:", ""]
            for author, message in self.thread[-MAX_MESSAGES:]:
                lines.append(f"  {author}: {_squash(message)[:MAX_MESSAGE_CHARS]}")

        lines += ["", "The message addressed to you:", "", f"  {_squash(self.instruction)}"]

        if self.document.strip():
            lines += [
                "",
                f'── "{self.document_name}" as it stands ──',
                "",
                self.document[:MAX_DOCUMENT_CHARS],
            ]

        lines += [
            "",
            "Answer in this shape:",
            "",
            "Your reply to the thread, as plain prose. No preamble, no sign-off.",
            "",
            "Then, **only if** you are proposing a change to the document, a line "
            "reading exactly `SUGGESTION:` on its own, followed by the complete "
            "document as it should read.",
            "",
            "How to decide:",
            "",
            "1. If you were asked a question, answer it and stop. Most messages in "
            "a thread are questions.",
            "2. If you were asked to change the document, say briefly what you "
            "changed and why, then give the whole document after `SUGGESTION:`. "
            "Change only what was asked for — the diff will show every line you "
            "touched, and this is somebody's document.",
            "3. If you cannot tell what is being asked, ask. A guess that rewrites "
            "a shared document is worse than a question.",
            "",
            "Nothing you write is applied automatically. A suggestion is offered to "
            "the people in the thread, who accept or decline it, so there is no "
            "reason to hedge a change you think is right — and no reason to make "
            "one you are unsure of.",
        ]
        return "\n".join(lines).strip()

    @staticmethod
    def parse(answer: str) -> ThreadAnswer:
        """Split the reply from the document it may propose.

        A missing or malformed ``SUGGESTION:`` line means no
        suggestion — never a partial document. Treating an unparseable
        answer as replacement text would hand the Accept button
        something that was never meant to be the document, and Accept
        writes.
        """
        text = str(answer or "").strip()
        if not text:
            return ThreadAnswer()

        match = _SUGGESTION.search(text)
        if match is None:
            return ThreadAnswer(body=_clip(text))

        body = text[: match.start()].strip()
        suggestion = match.group("body").strip()
        # A suggestion that is not a document is not a suggestion. The
        # model sometimes writes the marker and then explains itself.
        if len(suggestion) < 20:
            return ThreadAnswer(body=_clip(text))
        return ThreadAnswer(body=_clip(body), suggestion=suggestion)


def _clip(text: str) -> str:
    body = str(text or "").strip()
    if len(body) <= MAX_REPLY_CHARS:
        return body
    return body[:MAX_REPLY_CHARS].rsplit(" ", 1)[0] + "…"
