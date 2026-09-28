"""Who a reply is addressed to, read out of the reply.

A comment thread has two kinds of participant and the difference
matters more here than it does in a chat: the people are colleagues
who will read the reply when they next look, and ``@igni`` is the
agent, which will *act* on it. Sending "can you add the denylist
sentence" to the first is a request; sending it to the second writes a
version.

So addressing is explicit and it is in the text. A reply with no
``@igni`` never reaches a model, whatever it says — an agent that
decided for itself when a message was meant for it would turn every
aside between two colleagues into a possible edit.

The handle is the local part of an email, because that is what people
type. ``dana@acme.test`` is ``@dana``, and nobody writes
``@dana@acme.test``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The handle that addresses the agent. Reserved: a person whose email
#: local part is "igni" cannot be mentioned, and that is the right way
#: round — the cost of a missed human mention is a colleague not being
#: notified, and the cost of a missed agent mention is an instruction
#: silently becoming a note.
AGENT_HANDLE = "igni"

#: ``@`` at a word boundary, then a handle.
#:
#: The leading boundary is load-bearing. Without it every email
#: address in a reply reads as a mention of its own domain —
#: "forward it to dana@acme.test" would address ``@acme``.
_MENTION = re.compile(r"(?<![\w@.])@([A-Za-z0-9][\w.\-]*[A-Za-z0-9]|[A-Za-z0-9])")


def handle_for(author: str) -> str:
    """The handle that addresses this author.

    The local part of an email, lowercased. A name that is not an
    email is slugged, so "Dana Okonkwo" is ``@dana-okonkwo`` rather
    than being unaddressable.
    """
    text = str(author or "").strip().lower()
    if not text:
        return ""
    if "@" in text:
        text = text.split("@", 1)[0]
    slug = re.sub(r"[^a-z0-9.\-]+", "-", text).strip("-.")
    return slug


def parse_mentions(body: str) -> list[str]:
    """Every handle addressed in this text, in order, without repeats.

    Lowercased, because ``@Dana`` and ``@dana`` are the same person
    and a reply that reached one of them and not the other would be a
    bug nobody could see.
    """
    seen: list[str] = []
    for match in _MENTION.finditer(str(body or "")):
        handle = match.group(1).lower().strip(".-")
        if handle and handle not in seen:
            seen.append(handle)
    return seen


def addresses_agent(body: str) -> bool:
    """Whether this reply asks igni to do something.

    The single question that decides whether a reply is a note or an
    instruction, so it is asked of the text and nothing else.
    """
    return AGENT_HANDLE in parse_mentions(body)


@dataclass
class Participant:
    """Somebody who can be addressed in a thread."""

    handle: str
    #: What to show. The email, or the agent's name.
    label: str
    #: The email, empty for the agent.
    author: str = ""
    is_agent: bool = False


@dataclass
class Addressed:
    """Who a reply reaches, and who it names but cannot reach."""

    people: list[Participant] = field(default_factory=list)
    agent: bool = False
    #: Handles that match nobody in the thread.
    #:
    #: Surfaced rather than dropped. A reply addressed to ``@dna`` is
    #: a reply nobody gets, and silently posting it leaves the author
    #: believing they asked somebody a question.
    unknown: list[str] = field(default_factory=list)


def participants_of(authors: list[str]) -> list[Participant]:
    """The addressable people in a thread, plus the agent.

    The agent is always last and always present: it is the one
    participant that does not have to have spoken yet.
    """
    out: list[Participant] = []
    seen: set[str] = set()
    for author in authors:
        handle = handle_for(author)
        if not handle or handle in seen or handle == AGENT_HANDLE:
            continue
        seen.add(handle)
        out.append(Participant(handle=handle, label=author, author=author))
    out.append(Participant(handle=AGENT_HANDLE, label="igni", is_agent=True))
    return out


def resolve(body: str, authors: list[str]) -> Addressed:
    """Read a reply and say who it reaches."""
    known = {p.handle: p for p in participants_of(authors)}
    found = Addressed()
    for handle in parse_mentions(body):
        if handle == AGENT_HANDLE:
            found.agent = True
            continue
        person = known.get(handle)
        if person is not None:
            found.people.append(person)
        else:
            found.unknown.append(handle)
    return found
