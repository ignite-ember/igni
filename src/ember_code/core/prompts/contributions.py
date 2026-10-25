"""What extensions have done to the agent's prompts, right now.

For each prompt — the main prompt, plan mode, any agent — an extension
either **overrides** it with a whole prompt of its own, or **edits**
it: appends a section, inserts one before or after a named section, or
replaces a named section (:mod:`ember_code.core.prompts.document`).
Each may hold only while a condition the extension reports holds ("my
index covers this commit"). This is the table all of that is registered
into, and the prompt builder, the plan-mode block and the agent pool
read from.

``core`` owns it and the extension system registers into it, which
keeps the dependency pointing the right way: ``core`` does not import
the extension system, and knows no extension by name.

## Composing

An override is the new starting point. Every other extension's edits
apply on top of it, in the order a super admin set (:meth:`set_order`):
an append always lands; an insert or a replace lands when the override
has the section it names, and is skipped when it does not. An
``exclusive`` override takes no edits from anybody else.

**Who may do what is not decided here.** Two extensions replacing one
section, two overriding one prompt, an exclusive override against
someone's edits — those are collisions a super admin resolves on igni
server's Services page, and :mod:`ember_code.extensions.impact` applies
the decision before anything reaches this table. Should two claims
arrive anyway, the one earlier in the order keeps it and the other is
logged — never a merge.

**Conditions are reported, not polled.** An extension's process says
``notifications/igni/conditions`` when one changes; the table records
it and tells whoever watches, which rebuilds the agent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.prompts.document import (
    FRONTMATTER,
    EditRefused,
    Position,
    PromptDocument,
    PromptKind,
    PromptTarget,
)

logger = logging.getLogger(__name__)


class EditOp(StrEnum):
    #: A new section at the end.
    APPEND = "append"
    #: A new section before or after a named one.
    INSERT = "insert"
    #: A named section's text, replaced.
    REPLACE = "replace"


class PromptOverride(BaseModel):
    """One extension's whole prompt for one target."""

    model_config = ConfigDict(frozen=True)

    #: ``main``, ``plan_mode``, ``agent:<name>``.
    target: str
    text: str
    #: Takes no other extension's edits.
    exclusive: bool = False
    #: A condition the extension reports; ``None`` holds whenever it is ready.
    when: str | None = None
    #: Where the text was read from, when it is a file: an agent's
    #: definition is "from" there, which is how igni tells an
    #: extension's own agents (:mod:`ember_code.core.agents.tool_audience`).
    path: Path | None = None


class PromptEdit(BaseModel):
    """One change one extension makes to one target."""

    model_config = ConfigDict(frozen=True)

    target: str
    op: EditOp
    #: The section replaced, or the one an insert is placed against.
    section: str | None = None
    #: Before or after ``section``, for an insert.
    position: Position | None = None
    #: The new section's name, for an append or an insert.
    name: str | None = None
    text: str
    when: str | None = None

    def describe(self) -> str:
        if self.op is EditOp.REPLACE:
            return f"replacing {self.target}#{self.section}"
        if self.op is EditOp.INSERT and self.position is not None:
            return f"inserting {self.name} {self.position.value} {self.target}#{self.section}"
        return f"appending {self.name} to {self.target}"


class _Owner(BaseModel):
    name: str
    overrides: list[PromptOverride] = Field(default_factory=list)
    edits: list[PromptEdit] = Field(default_factory=list)
    holding: frozenset[str] = frozenset()

    def live(self, when: str | None) -> bool:
        return when is None or when in self.holding


class Composition(BaseModel):
    """One prompt as extensions have shaped it, before igni's own substitutions."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: PromptDocument
    #: The extension whose override this is built on, if any.
    override_owner: str | None = None
    #: The override's file, when it was one.
    override_path: Path | None = None
    #: The extension whose text the agent's frontmatter now is, if any.
    frontmatter_owner: str | None = None
    #: Edits that did not land, and why.
    skipped: list[str] = Field(default_factory=list)


class PromptContributions:
    """Every live override and edit, by the extension that made it."""

    def __init__(self) -> None:
        self._owners: dict[str, _Owner] = {}
        self._order: list[str] = []
        self._watchers: list[Callable[[], None]] = []

    # ── Registering ────────────────────────────────────────────────

    def set_owner(
        self,
        owner: str,
        name: str,
        *,
        overrides: list[PromptOverride] | None = None,
        edits: list[PromptEdit] | None = None,
    ) -> None:
        """What ``owner`` does now, replacing what it did before.

        Conditions already reported are kept: a restart re-registers
        what it does and its process re-reports anyway.
        """
        previous = self._owners.get(owner)
        holding = previous.holding if previous is not None else frozenset()
        self._owners[owner] = _Owner(
            name=name, overrides=overrides or [], edits=edits or [], holding=holding
        )
        self._changed()

    def remove_owner(self, owner: str) -> None:
        if self._owners.pop(owner, None) is not None:
            self._changed()

    def set_order(self, order: list[str]) -> None:
        """The order edits from several extensions apply in, as a super admin set it."""
        if order != self._order:
            self._order = list(order)
            self._changed()

    def hold(self, owner: str, conditions: dict[str, bool]) -> bool:
        """Record which of ``owner``'s conditions hold. ``True`` if anything moved."""
        entry = self._owners.get(owner)
        holding = set(entry.holding) if entry is not None else set()
        for name, holds in conditions.items():
            (holding.add if holds else holding.discard)(name)
        if entry is None:
            # Reported before what it does arrived — a process that
            # starts quickly. Kept for when it does.
            self._owners[owner] = _Owner(name=owner, holding=frozenset(holding))
            return False
        if frozenset(holding) == entry.holding:
            return False
        self._owners[owner] = entry.model_copy(update={"holding": frozenset(holding)})
        self._changed()
        return True

    def watch(self, callback: Callable[[], None]) -> None:
        """Be told whenever what the prompts say may have changed."""
        self._watchers.append(callback)

    def _changed(self) -> None:
        for watcher in self._watchers:
            try:
                watcher()
            except Exception:  # noqa: BLE001 — one watcher must not stop the record
                logger.exception("a prompt-contributions watcher failed")

    # ── Reading ────────────────────────────────────────────────────

    def _ordered(self) -> list[str]:
        rank = {owner: index for index, owner in enumerate(self._order)}
        return sorted(self._owners, key=lambda owner: (rank.get(owner, len(rank)), owner))

    def override(self, target: str) -> tuple[str, PromptOverride] | None:
        """The override in force for ``target``, and whose it is."""
        held: tuple[str, PromptOverride] | None = None
        for owner in self._ordered():
            entry = self._owners[owner]
            for override in entry.overrides:
                if override.target != target or not entry.live(override.when):
                    continue
                if held is None:
                    held = (owner, override)
                else:
                    logger.warning(
                        "%s and %s both override %s; %s keeps it — resolve the collision "
                        "on igni server's Services page",
                        held[0],
                        owner,
                        target,
                        held[0],
                    )
        return held

    def edits(self, target: str) -> list[tuple[str, PromptEdit]]:
        """The edits in force for ``target``, in the order they apply."""
        return [
            (owner, edit)
            for owner in self._ordered()
            for edit in self._owners[owner].edits
            if edit.target == target and self._owners[owner].live(edit.when)
        ]

    def agents(self) -> list[str]:
        """Every agent an extension overrides or edits right now."""
        names = {
            PromptTarget.parse(item.target).agent
            for entry in self._owners.values()
            for item in [*entry.overrides, *entry.edits]
            if item.target.startswith("agent:") and entry.live(item.when)
        }
        return sorted(name for name in names if name is not None)

    def compose(
        self,
        target: str,
        base: str | None,
        *,
        prepare: Callable[[str], str] | None = None,
    ) -> Composition | None:
        """``target`` as extensions have shaped it.

        ``base`` is igni's own text, or ``None`` when igni has none —
        an agent only an extension defines. ``prepare`` runs on whichever
        text is the starting point before it is read as sections, for
        includes like ``{{PERSISTING}}`` whose sections belong to it.
        """
        agent = PromptTarget.parse(target).kind is PromptKind.AGENT
        held = self.override(target)
        if held is not None:
            starting = held[1].text
        elif base is not None:
            starting = base
        else:
            return None
        if prepare is not None:
            starting = prepare(starting)
        composition = Composition(
            document=PromptDocument(starting, agent=agent),
            override_owner=held[0] if held else None,
            override_path=held[1].path if held else None,
            frontmatter_owner=held[0] if held and agent else None,
        )
        anchors = set(composition.document.sections())
        replaced: dict[str, str] = {}
        for owner, edit in self.edits(target):
            if held is not None and held[1].exclusive and owner != held[0]:
                composition.skipped.append(
                    f"{owner}: {edit.describe()} — {held[0]} overrides it exclusively"
                )
                continue
            if edit.op is not EditOp.APPEND and edit.section not in anchors:
                whose = f"{held[0]}'s override" if held else "it"
                composition.skipped.append(
                    f"{owner}: {edit.describe()} — {whose} has no section {edit.section!r}"
                )
                continue
            if edit.op is EditOp.REPLACE and edit.section in replaced:
                logger.warning(
                    "%s and %s both replace %s#%s; %s keeps it — resolve the collision "
                    "on igni server's Services page",
                    replaced[edit.section],
                    owner,
                    target,
                    edit.section,
                    replaced[edit.section],
                )
                continue
            try:
                composition.document = self._apply(composition.document, edit)
            except (EditRefused, ValueError) as exc:
                composition.skipped.append(f"{owner}: {edit.describe()} — {exc}")
                continue
            if edit.op is EditOp.REPLACE and edit.section is not None:
                replaced[edit.section] = owner
                if edit.section == FRONTMATTER and agent:
                    composition.frontmatter_owner = owner
        for line in composition.skipped:
            logger.info("prompt %s: %s", target, line)
        return composition

    @staticmethod
    def _apply(document: PromptDocument, edit: PromptEdit) -> PromptDocument:
        if edit.op is EditOp.REPLACE and edit.section is not None:
            return document.replace(edit.section, edit.text)
        if edit.op is EditOp.INSERT and edit.section is not None and edit.position is not None:
            return document.insert(edit.section, edit.position, edit.name or "added", edit.text)
        return document.append(edit.name or "added", edit.text)


#: The table the prompt builder, the plan-mode block and the agent
#: pool read when nobody passes one. Module scope because the thing
#: that registers and the things that read are wired at different
#: times, by different layers.
prompt_contributions = PromptContributions()

__all__ = [
    "Composition",
    "EditOp",
    "PromptContributions",
    "PromptEdit",
    "PromptOverride",
    "prompt_contributions",
]
