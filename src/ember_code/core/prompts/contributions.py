"""What extensions have done to the agent's prompts, right now.

An extension may replace a whole prompt, replace one of its sections,
add to a slot, or stand in for one of igni's agents — and each of
those may hold only while a condition the extension reports holds
("my index covers this commit"). This is the table all of that is
registered into, and the prompt builder, the plan-mode block and the
agent loader read from.

``core`` owns it and the extension system registers into it, which
keeps the dependency pointing the right way: ``core`` does not import
the extension system, and knows no extension by name.

**Who wins is not decided here.** Two extensions both replacing the
same section is a collision a super admin resolves on igni server's
Services page, and :mod:`ember_code.extensions.impact` applies that
decision before anything reaches this table. What arrives is already
one claim per exclusive target. Should two arrive anyway, the one
registered first keeps it and the other is logged — never a merge.

**Conditions are reported, not polled.** An extension's process says
``notifications/igni/conditions`` when one changes; the table records
it and tells whoever watches, which rebuilds the agent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ember_code.core.prompts.document import (
    MAX_SLOT_CHARS,
    PromptDocumentName,
    PromptTarget,
)

logger = logging.getLogger(__name__)


class PromptClaimKind(StrEnum):
    #: The whole prompt, or one section of it.
    REPLACE = "replace"
    #: Text added to a slot, beside other extensions'.
    SLOT = "slot"
    #: One of igni's agents, by name, defined by a file the extension ships.
    AGENT = "agent"


class PromptClaim(BaseModel):
    """One thing one extension does to a prompt."""

    model_config = ConfigDict(frozen=True)

    kind: PromptClaimKind
    #: A prompt target for ``replace`` and ``slot``; an agent's name for ``agent``.
    target: str
    #: The text, for ``replace`` and ``slot``.
    text: str = ""
    #: The agent's definition, for ``agent``.
    path: Path | None = None
    #: A condition the extension reports; ``None`` holds whenever the
    #: extension is ready.
    when: str | None = None


class _Owner(BaseModel):
    name: str
    claims: list[PromptClaim]
    holding: frozenset[str] = frozenset()


class AgentReplacement(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner: str
    path: Path


class PromptContributions:
    """Every live claim, by the extension that made it."""

    def __init__(self) -> None:
        self._owners: dict[str, _Owner] = {}
        self._watchers: list[Callable[[], None]] = []

    # ── Registering ────────────────────────────────────────────────

    def set_owner(self, owner: str, name: str, claims: list[PromptClaim]) -> list[str]:
        """What ``owner`` claims now, replacing what it claimed before.

        Returns the claims refused, with why — a slot fill longer than
        a slot holds. Conditions already reported are kept: a restart
        re-registers its claims and its process re-reports anyway.
        """
        refused: list[str] = []
        kept: list[PromptClaim] = []
        for claim in claims:
            if claim.kind is PromptClaimKind.SLOT and len(claim.text) > MAX_SLOT_CHARS:
                refused.append(
                    f"slot {claim.target}: {len(claim.text)} characters, and a slot "
                    f"holds {MAX_SLOT_CHARS}; replace a section to say more"
                )
                continue
            kept.append(claim)
        previous = self._owners.get(owner)
        holding = previous.holding if previous is not None else frozenset()
        self._owners[owner] = _Owner(name=name, claims=kept, holding=holding)
        self._changed()
        return refused

    def remove_owner(self, owner: str) -> None:
        if self._owners.pop(owner, None) is not None:
            self._changed()

    def hold(self, owner: str, conditions: dict[str, bool]) -> bool:
        """Record which of ``owner``'s conditions hold. ``True`` if anything moved."""
        entry = self._owners.get(owner)
        holding = set(entry.holding) if entry is not None else set()
        for name, holds in conditions.items():
            (holding.add if holds else holding.discard)(name)
        if entry is None:
            # Reported before its claims arrived — a process that
            # starts quickly. Kept for when they do.
            self._owners[owner] = _Owner(name=owner, claims=[], holding=frozenset(holding))
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

    def _live(self, kind: PromptClaimKind) -> list[tuple[str, str, PromptClaim]]:
        """``(owner, name, claim)`` for every claim in force, in owner order."""
        live = []
        for owner in sorted(self._owners):
            entry = self._owners[owner]
            for claim in entry.claims:
                if claim.kind is not kind:
                    continue
                if claim.when is not None and claim.when not in entry.holding:
                    continue
                live.append((owner, entry.name, claim))
        return live

    def _exclusive(self, kind: PromptClaimKind) -> dict[str, tuple[str, PromptClaim]]:
        taken: dict[str, tuple[str, PromptClaim]] = {}
        for owner, _, claim in self._live(kind):
            held = taken.get(claim.target)
            if held is not None:
                logger.warning(
                    "%s and %s both claim %s %s; %s keeps it — resolve the collision "
                    "on igni server's Services page",
                    held[0],
                    owner,
                    kind.value,
                    claim.target,
                    held[0],
                )
                continue
            taken[claim.target] = (owner, claim)
        return taken

    def replacement(self, document: PromptDocumentName) -> str | None:
        """The whole of ``document``, when an extension replaces it."""
        held = self._exclusive(PromptClaimKind.REPLACE).get(document.value)
        return held[1].text if held is not None else None

    def section_replacements(self, document: PromptDocumentName) -> dict[str, str]:
        """``{section: text}`` for every section of ``document`` an extension replaces."""
        sections: dict[str, str] = {}
        for target, (_, claim) in self._exclusive(PromptClaimKind.REPLACE).items():
            parsed = PromptTarget.parse(target)
            if parsed.document is document and parsed.part is not None:
                sections[parsed.part] = claim.text
        return sections

    def slot_fills(self, document: PromptDocumentName) -> dict[str, str]:
        """``{slot: text}``: every live fill of each slot, stacked.

        One fill is its text. Several are each headed by the extension
        that wrote it, in extension-id order, so the order does not
        depend on which started first.
        """
        by_slot: dict[str, list[tuple[str, str]]] = {}
        for _, name, claim in self._live(PromptClaimKind.SLOT):
            parsed = PromptTarget.parse(claim.target)
            if parsed.document is document and parsed.part is not None:
                by_slot.setdefault(parsed.part, []).append((name, claim.text.strip()))
        fills: dict[str, str] = {}
        for slot, texts in by_slot.items():
            if len(texts) == 1:
                fills[slot] = texts[0][1]
            else:
                fills[slot] = "\n\n".join(f"### From {name}\n\n{text}" for name, text in texts)
        return fills

    def agent_replacements(self) -> dict[str, AgentReplacement]:
        """``{agent: replacement}`` for every agent an extension stands in for."""
        return {
            target: AgentReplacement(owner=owner, path=claim.path)
            for target, (owner, claim) in self._exclusive(PromptClaimKind.AGENT).items()
            if claim.path is not None
        }


#: The table the prompt builder, the plan-mode block and the agent
#: loader read when nobody passes one. Module scope because the thing
#: that registers and the things that read are wired at different
#: times, by different layers.
prompt_contributions = PromptContributions()

__all__ = [
    "AgentReplacement",
    "PromptClaim",
    "PromptClaimKind",
    "PromptContributions",
    "prompt_contributions",
]
