"""A prompt, as the named sections and slots an extension can reach.

An extension may take over any part of the agent's prompt — the whole
of it, one named section, or the text in a slot. That needs names that
do not move when the prose does, so the markdown says them:

    <!-- section:workflow -->
    ## Plan, Align, Build
    ...
    <!-- /section:workflow -->

    <!-- slot:code_intelligence -->
    (what a slot says when nothing fills it, often nothing)
    <!-- /slot:code_intelligence -->

**A section** is igni's text that an extension may replace outright.
**A slot** is a place igni leaves for extensions to add to: what is
between its markers is the default, shown only while nothing fills it,
which is how a prompt says "there is nowhere to file project knowledge"
without naming the extension that would have been the place.

Whether a slot is filled is a conditional of its own,
``<!-- slot.<id>:only -->`` and ``<!-- slot.<id>:absent -->``, for
guidance that depends on it and does not sit beside it.

A target is spelled ``<document>`` (the whole prompt), or
``<document>.<id>`` for a section or a slot. ``docs/prompt-targets.json``
lists every one igni has; ``tests/test_prompt_targets_are_listed.py``
keeps it true.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: A section's or a slot's own name: lower snake case.
NAME = r"[a-z][a-z0-9_]*"

_SECTION = re.compile(rf"<!-- section:({NAME}) -->(.*?)<!-- /section:\1 -->", re.S)
_SLOT = re.compile(rf"<!-- slot:({NAME}) -->(.*?)<!-- /slot:\1 -->", re.S)
_OPEN = re.compile(rf"<!-- (section|slot):({NAME}) -->")
_CLOSE = re.compile(rf"<!-- /(section|slot):({NAME}) -->")

#: The longest text one extension may put in one slot. A slot sits
#: inside igni's prompt, beside other extensions' text; replacing a
#: section or the whole prompt is the way to say more.
MAX_SLOT_CHARS = 8_000


class PromptDocumentName(StrEnum):
    """The prompts an extension can reach, by the name a target starts with."""

    #: The main agent's system prompt.
    MAIN = "main"
    #: What the main agent is told about plan mode.
    PLAN_MODE = "plan_mode"

    @property
    def template(self) -> str:
        """The prompt file it is loaded from, by ``load_prompt`` name."""
        return _TEMPLATES[self]


_TEMPLATES = {
    PromptDocumentName.MAIN: "main_agent",
    PromptDocumentName.PLAN_MODE: "plan_mode.base",
}


class PromptTarget(BaseModel):
    """``main``, ``main.workflow`` or ``plan_mode.code_intelligence``, parsed."""

    model_config = ConfigDict(frozen=True)

    document: PromptDocumentName
    #: The section or slot, or ``None`` for the whole document.
    part: str | None = None

    @classmethod
    def parse(cls, spelled: str) -> PromptTarget:
        document, dot, part = spelled.partition(".")
        if dot and not re.fullmatch(NAME, part):
            raise ValueError(f"{spelled!r} is not a prompt target")
        return cls(document=PromptDocumentName(document), part=part or None)

    @property
    def whole(self) -> bool:
        return self.part is None

    def __str__(self) -> str:
        return self.document.value if self.part is None else f"{self.document.value}.{self.part}"


class PromptDocument:
    """One prompt's text, read as sections and slots."""

    def __init__(self, text: str) -> None:
        self._check_balanced(text)
        self.text = text

    @staticmethod
    def _check_balanced(text: str) -> None:
        """Refuse a marker without its partner.

        A section whose closing marker went missing would swallow the
        rest of the prompt when it is replaced, and a replacement is
        exactly when nobody is reading the result.
        """
        opened = sorted(m.groups() for m in _OPEN.finditer(text))
        closed = sorted(m.groups() for m in _CLOSE.finditer(text))
        if opened != closed:
            raise ValueError(
                f"unbalanced section or slot markers: opened {opened}, closed {closed}"
            )

    def sections(self) -> list[str]:
        return [m.group(1) for m in _SECTION.finditer(self.text)]

    def slots(self) -> list[str]:
        return [m.group(1) for m in _SLOT.finditer(self.text)]

    def replace_sections(self, replacements: dict[str, str]) -> PromptDocument:
        """These sections' text replaced, markers and all.

        The replacement may carry markers of its own — a slot, another
        section — and they work as igni's do. A name this document has
        no section for is left alone: :class:`PromptTargets` is where
        an unknown target is reported, not here.
        """

        def swap(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in replacements:
                return match.group(0)
            return replacements[name].strip()

        return PromptDocument(_SECTION.sub(swap, self.text))

    def fill_slots(self, fills: dict[str, str]) -> str:
        """The text with every slot resolved: its fill, or its default.

        Section markers go too. This is the last thing done to the
        document's own structure; what remains is prose and the
        host's conditionals.
        """
        text = _SLOT.sub(lambda m: fills.get(m.group(1), m.group(2)).strip(), self.text)
        text = re.sub(rf"<!-- /?section:{NAME} -->\n?", "", text)
        # An empty slot leaves the blank lines that surrounded it.
        return re.sub(r"\n{3,}", "\n\n", text)


class PromptTargets(BaseModel):
    """Every target igni's prompts have — what an extension may name."""

    #: ``main``, ``plan_mode``.
    documents: list[str] = Field(default_factory=list)
    #: ``main.workflow``, ...
    sections: list[str] = Field(default_factory=list)
    #: ``main.code_intelligence``, ...
    slots: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, documents: dict[PromptDocumentName, str]) -> PromptTargets:
        targets = cls()
        for name, text in documents.items():
            document = PromptDocument(text)
            targets.documents.append(name.value)
            targets.sections += [f"{name.value}.{s}" for s in document.sections()]
            targets.slots += [f"{name.value}.{s}" for s in document.slots()]
        return targets

    @classmethod
    def current(cls, prompts_dir: Path) -> PromptTargets:
        """igni's targets, from the prompt files as they ship.

        ``main`` includes ``persisting.md``, which it pulls in at
        ``{{PERSISTING}}``: its slots are the main prompt's slots.
        """
        main = (prompts_dir / "main_agent.md").read_text(encoding="utf-8")
        persisting = (prompts_dir / "persisting.md").read_text(encoding="utf-8")
        plan_mode = (prompts_dir / "plan_mode.base.md").read_text(encoding="utf-8")
        return cls.of(
            {
                PromptDocumentName.MAIN: main.replace("{{PERSISTING}}", persisting),
                PromptDocumentName.PLAN_MODE: plan_mode,
            }
        )

    def knows(self, target: PromptTarget, *, slot: bool) -> bool:
        spelled = str(target)
        if target.whole:
            return not slot and spelled in self.documents
        return spelled in (self.slots if slot else self.sections)


__all__ = [
    "MAX_SLOT_CHARS",
    "PromptDocument",
    "PromptDocumentName",
    "PromptTarget",
    "PromptTargets",
]
