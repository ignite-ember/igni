"""A prompt, as the named sections an extension can reach.

An extension either **overrides** a prompt — brings its own, whole —
or **edits** it: appends a section at the end, inserts one before or
after a named section, or replaces a named section. That needs names
that do not move when the prose does, so the markdown says them:

    <!-- section:workflow -->
    ## Plan, Align, Build
    ...
    <!-- /section:workflow -->

Sections are flat: one never opens inside another. An agent's prompt
is its whole ``.md`` file, and its YAML header is a section too,
``frontmatter``, so changing what an agent is described as, or which
tools it has, is replacing a section like any other.

A section an extension adds is marked ``<!-- added:<id> -->`` rather
than ``section:``: edits anchor to the sections of the prompt they were
written against — igni's, or the override's — never to another
extension's additions, so two extensions never need to agree on names.

A target is spelled ``main``, ``plan_mode`` or ``agent:<name>``, and a
section of one ``<target>#<section>``. ``docs/prompt-targets.json``
lists every target and section igni ships;
``tests/test_prompt_targets_are_listed.py`` keeps it true.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: A section's own name: lower snake case.
NAME = r"[a-z][a-z0-9_]*"

#: An agent's name, as the agent pool knows it — a plugin's agents
#: carry their plugin's name, ``acme.notes:archivist``.
AGENT = r"[a-z0-9][a-z0-9_.:-]*"

#: ``main``, ``plan_mode``, ``agent:<name>``; ``#<section>`` for one section.
TARGET = re.compile(rf"^(main|plan_mode|agent:{AGENT})(#{NAME})?$")

#: The agent header's section name.
FRONTMATTER = "frontmatter"

_SECTION = re.compile(rf"<!-- section:({NAME}) -->(.*?)<!-- /section:\1 -->", re.S)
_MARKER = re.compile(rf"<!-- (/?)(section|added):({NAME}) -->")
_HEADER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n?", re.S)
_ADDED_NEXT = re.compile(rf"\s*<!-- added:({NAME}) -->.*?<!-- /added:\1 -->", re.S)


class EditRefused(ValueError):
    """An edit the prompt it was aimed at cannot take; the message says why."""


class PromptKind(StrEnum):
    """The prompts an extension can reach."""

    #: The main agent's system prompt.
    MAIN = "main"
    #: What the main agent is told about plan mode.
    PLAN_MODE = "plan_mode"
    #: One agent's prompt: its whole ``.md`` file.
    AGENT = "agent"


class Position(StrEnum):
    BEFORE = "before"
    AFTER = "after"


class PromptTarget(BaseModel):
    """``main``, ``agent:explorer`` or ``main#workflow``, parsed."""

    model_config = ConfigDict(frozen=True)

    kind: PromptKind
    #: The agent's name, for ``agent:``.
    agent: str | None = None
    #: The section, or ``None`` for the whole prompt.
    section: str | None = None

    @classmethod
    def parse(cls, spelled: str) -> PromptTarget:
        if not TARGET.fullmatch(spelled):
            raise ValueError(f"{spelled!r} is not a prompt target")
        document, _, section = spelled.partition("#")
        if document.startswith("agent:"):
            return cls(kind=PromptKind.AGENT, agent=document[6:], section=section or None)
        return cls(kind=PromptKind(document), section=section or None)

    @property
    def document(self) -> str:
        """The whole prompt this names: ``main``, ``agent:explorer``."""
        return f"agent:{self.agent}" if self.kind is PromptKind.AGENT else self.kind.value

    @property
    def whole(self) -> bool:
        return self.section is None

    def __str__(self) -> str:
        return self.document if self.section is None else f"{self.document}#{self.section}"


class PromptDocument:
    """One prompt's text, read as sections.

    ``agent=True`` for an agent's file: its YAML header is the
    ``frontmatter`` section. Every method returns a new document.
    """

    def __init__(self, text: str, *, agent: bool = False) -> None:
        self.agent = agent
        header = _HEADER.match(text) if agent else None
        self.frontmatter = header.group(0) if header else None
        self.body = text[header.end() :] if header else text
        self._check_markers(self.body)

    @staticmethod
    def _check_markers(text: str) -> None:
        """Refuse a marker without its partner, and a section inside another.

        A section whose closing marker went missing would swallow the
        rest of the prompt when it is replaced, and a replacement is
        exactly when nobody is reading the result.
        """
        open_: tuple[str, str] | None = None
        for match in _MARKER.finditer(text):
            closing, kind, name = match.group(1) == "/", match.group(2), match.group(3)
            if not closing:
                if open_ is not None:
                    raise ValueError(f"{kind}:{name} opens inside {open_[0]}:{open_[1]}")
                open_ = (kind, name)
            elif open_ != (kind, name):
                raise ValueError(f"/{kind}:{name} closes nothing that is open")
            else:
                open_ = None
        if open_ is not None:
            raise ValueError(f"{open_[0]}:{open_[1]} is never closed")

    @property
    def text(self) -> str:
        return (self.frontmatter or "") + self.body

    def _with(self, *, frontmatter: str | None = None, body: str | None = None) -> PromptDocument:
        document = PromptDocument.__new__(PromptDocument)
        document.agent = self.agent
        document.frontmatter = self.frontmatter if frontmatter is None else frontmatter
        document.body = self.body if body is None else body
        self._check_markers(document.body)
        return document

    def sections(self) -> list[str]:
        """Its own sections, in order — never what an extension added."""
        names = [FRONTMATTER] if self.frontmatter is not None else []
        return names + [m.group(1) for m in _SECTION.finditer(self.body)]

    def replace(self, section: str, text: str) -> PromptDocument:
        """``section``'s text replaced by ``text``, under the same name."""
        if section == FRONTMATTER and self.agent:
            header = text.strip() + "\n"
            if not _HEADER.fullmatch(header):
                raise EditRefused("an agent's frontmatter is a YAML block between '---' lines")
            return self._with(frontmatter=header)
        match = self._find(section)
        replaced = f"<!-- section:{section} -->\n{text.strip()}\n<!-- /section:{section} -->"
        return self._with(body=self.body[: match.start()] + replaced + self.body[match.end() :])

    def insert(self, anchor: str, position: Position, name: str, text: str) -> PromptDocument:
        """A new section ``name`` placed before or after ``anchor``."""
        added = self._added(name, text)
        if anchor == FRONTMATTER and self.agent and self.frontmatter is not None:
            if position is Position.BEFORE:
                raise EditRefused("nothing goes before an agent's frontmatter")
            end = self._past_added(0)
            return self._with(body=self.body[:end] + "\n\n" + added + "\n\n" + self.body[end:])
        match = self._find(anchor)
        if position is Position.BEFORE:
            # Right before the anchor, so after what was put before it
            # earlier: insertions keep the order they were made in.
            body = self.body[: match.start()] + added + "\n\n" + self.body[match.start() :]
        else:
            end = self._past_added(match.end())
            body = self.body[:end] + "\n\n" + added + self.body[end:]
        return self._with(body=body)

    def _past_added(self, index: int) -> int:
        """Past the sections already added right after ``index``, for the same reason."""
        while True:
            following = _ADDED_NEXT.match(self.body, index)
            if following is None:
                return index
            index = following.end()

    def append(self, name: str, text: str) -> PromptDocument:
        """A new section ``name`` at the end."""
        return self._with(body=self.body.rstrip() + "\n\n" + self._added(name, text) + "\n")

    def render(self) -> str:
        """The text with every marker gone: what the model reads."""
        return (self.frontmatter or "") + self.unmarked(self.body)

    @staticmethod
    def unmarked(text: str) -> str:
        """``text`` without section markers — for prompt text read straight from a file."""
        # A removed marker leaves the blank lines that surrounded it.
        return re.sub(r"\n{3,}", "\n\n", _MARKER.sub("", text)).strip()

    def _find(self, section: str) -> re.Match[str]:
        for match in _SECTION.finditer(self.body):
            if match.group(1) == section:
                return match
        raise EditRefused(f"there is no section {section!r}")

    @staticmethod
    def _added(name: str, text: str) -> str:
        return f"<!-- added:{name} -->\n{text.strip()}\n<!-- /added:{name} -->"


class PromptTargets(BaseModel):
    """Every target and section igni's prompts have — what an extension may name."""

    #: ``main``, ``plan_mode``, ``agent:explorer``, ...
    documents: list[str] = Field(default_factory=list)
    #: ``main#workflow``, ``agent:explorer#frontmatter``, ...
    sections: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, documents: dict[str, PromptDocument]) -> PromptTargets:
        targets = cls()
        for name, document in documents.items():
            targets.documents.append(name)
            targets.sections += [f"{name}#{s}" for s in document.sections()]
        return targets

    @classmethod
    def current(cls, prompts_dir: Path, agents_dir: Path) -> PromptTargets:
        """igni's targets, from the prompt files and bundled agents as they ship.

        ``main`` includes ``persisting.md``, which it pulls in at
        ``{{PERSISTING}}``: its sections are the main prompt's.
        """
        main = (prompts_dir / "main_agent.md").read_text(encoding="utf-8")
        persisting = (prompts_dir / "persisting.md").read_text(encoding="utf-8")
        documents = {
            PromptKind.MAIN.value: PromptDocument(main.replace("{{PERSISTING}}", persisting)),
            PromptKind.PLAN_MODE.value: PromptDocument(
                (prompts_dir / "plan_mode.base.md").read_text(encoding="utf-8")
            ),
        }
        for path in sorted(agents_dir.glob("*.md")):
            documents[f"agent:{path.stem}"] = PromptDocument(
                path.read_text(encoding="utf-8"), agent=True
            )
        return cls.of(documents)

    def has(self, target: PromptTarget) -> bool:
        spelled = str(target)
        return spelled in (self.documents if target.whole else self.sections)


__all__ = [
    "AGENT",
    "FRONTMATTER",
    "NAME",
    "TARGET",
    "EditRefused",
    "Position",
    "PromptDocument",
    "PromptKind",
    "PromptTarget",
    "PromptTargets",
]
