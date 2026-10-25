"""What an extension changes in igni, and where two extensions collide.

An extension can take over a great deal — the whole of the agent's
prompt, if that is what a deployment wants. That is allowed on
purpose, and it makes one question unavoidable: what happens when two
extensions want the same thing? This module answers it, and it is
the same answer in three places — the Services page on igni server,
where a super admin sees an extension's impact before it is
distributed; igni server's distribution, which holds a version back
until its collisions are decided; and the host, which applies the
decisions when it loads.

## The impact

:class:`ExtensionImpact` is everything a manifest changes, read off it:
the prompts it overrides and the edits it makes to them, its commands
and anchors, and — for review rather than for collisions — its pages,
menu entries, indicators, settings, tools (each with the four hints
igni's permission modes decide on), host-model grants, RPC grants,
egress and secrets.

## Prompts: overrides and edits

Per prompt — ``main``, ``plan_mode``, ``agent:<name>`` — an extension
either overrides it whole or edits it: appends a section, inserts one
before or after a named section, replaces a named section. Edits from
several extensions stack, in the order a super admin set, and never
collide with each other unless they replace the same section.

An override is the new starting point, not a rival to the edits: the
others' appends land on it, and their inserts and replacements land
where it has the section they name. Where it does not, they are
**dropped** — not a collision, nothing to decide — and the drop is
listed (:attr:`ResolvedImpact.dropped`), because an override declares
its sections and the gap is visible before anything ships.

## What collides

A **claim** is something only one extension can have, spelled
``<kind>:<name>``:

* ``prompt:<target>`` — overriding a whole prompt. Two overrides of one
  prompt collide. An ``exclusive`` override also collides with every
  other extension that edits that prompt at all: the super admin picks
  the override (their edits are off) or the edits (the override is
  off — and so is any other override in that collision);
* ``prompt:<target>#<section>`` — replacing one section;
* ``command:/<name>`` — a slash command;
* ``anchor:<NAME>`` — the text at a ``{{NAME}}`` in a prompt.

An insert or an append is spelled ``prompt:<target>+<name>`` where an
item has to be named — switched off, dropped — and is never a claim.

**igni's own commands** are not anybody's to claim. An extension
declaring ``/clear`` does not collide with another extension, it
collides with igni, and there is nothing for a super admin to decide:
igni's command runs first and the extension's never would. igni server
refuses such a bundle at upload; the host, which may be a newer igni
with a command the bundle did not know about, switches the claim off
(:class:`IgniCommands`, passed to :class:`ImpactResolver` as
``reserved``). ``docs/igni-commands.json`` lists them for authors and
for igni server, and a test holds it to the dispatcher.

**Not collisions**, because they cannot be: inserts and appends stack;
tools are registered under the extension's server name; pages and
indicators are named in the extension's own namespace; settings are
stored per extension; a plugin's own agents load under the extension's
id; grants and egress only add. A condition (``when``) does not avoid a
collision either — two extensions that would replace one section under
different conditions still claim it.

## Deciding

A super admin picks the winner of each collision on igni server. A
:class:`Resolution` names the claim, the extensions it was between,
and the winner, with who decided and when. It applies while the same
extensions are in the collision; a third extension joining it is a new
collision. The losers' part in it is switched off — the rest of each
loser still works.

**Undecided**, the host switches it off for every extension in the
collision (:meth:`ImpactResolver.enforce`), and says why. Listing for a
super admin keeps undecided claims in play (:meth:`ImpactResolver.survey`),
so every collision there is to decide is shown at once.

``tests/fixtures/extension_impact_cases.json`` holds cases igni server
runs too: two implementations of one rule, held to the same answers.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.mcp.hints import ToolHints
from ember_code.core.prompts.contributions import EditOp
from ember_code.core.prompts.document import FRONTMATTER, Position
from ember_code.extensions.manifest import ExtensionManifest, PromptEditContribution


class ClaimKind(StrEnum):
    PROMPT = "prompt"
    COMMAND = "command"
    ANCHOR = "anchor"


class Claim:
    """Spelling and reading claim names. One place, because igni server reads them too."""

    @staticmethod
    def prompt(target: str) -> str:
        """``prompt:main`` for an override, ``prompt:main#workflow`` for a section."""
        return f"{ClaimKind.PROMPT.value}:{target}"

    @staticmethod
    def addition(target: str, name: str) -> str:
        """``prompt:main+notes``: an insert or an append. Named, never claimed."""
        return f"{ClaimKind.PROMPT.value}:{target}+{name}"

    @staticmethod
    def command(command: str) -> str:
        return f"{ClaimKind.COMMAND.value}:{command}"

    @staticmethod
    def anchor(name: str) -> str:
        return f"{ClaimKind.ANCHOR.value}:{name}"

    @staticmethod
    def whole_prompt(claim: str) -> str | None:
        """``main`` for ``prompt:main``; ``None`` for anything else."""
        kind, _, name = claim.partition(":")
        if kind != ClaimKind.PROMPT.value or "#" in name or "+" in name:
            return None
        return name

    @staticmethod
    def prompt_document(item: str) -> str | None:
        """``main`` for ``prompt:main``, ``prompt:main#workflow`` and ``prompt:main+notes``."""
        kind, _, name = item.partition(":")
        if kind != ClaimKind.PROMPT.value:
            return None
        return name.partition("#")[0].partition("+")[0]

    @staticmethod
    def is_edit(item: str) -> bool:
        kind, _, name = item.partition(":")
        return kind == ClaimKind.PROMPT.value and ("#" in name or "+" in name)


class ImpactOverride(BaseModel):
    """A whole prompt an extension brings."""

    model_config = ConfigDict(frozen=True)

    target: str
    #: Its sections; an agent's ``frontmatter`` is implied.
    sections: list[str]
    exclusive: bool = False
    when: str | None = None

    @property
    def key(self) -> str:
        return Claim.prompt(self.target)

    def has(self, section: str) -> bool:
        return section in self.sections or (
            section == FRONTMATTER and self.target.startswith("agent:")
        )


class ImpactEdit(BaseModel):
    """One change an extension makes to a prompt."""

    model_config = ConfigDict(frozen=True)

    target: str
    op: EditOp
    #: The section replaced, or inserted against.
    section: str | None = None
    position: Position | None = None
    #: The new section's name, for an insert or an append.
    name: str | None = None
    when: str | None = None

    @property
    def key(self) -> str:
        if self.op is EditOp.REPLACE:
            return Claim.prompt(f"{self.target}#{self.section}")
        return Claim.addition(self.target, self.name or "")

    @classmethod
    def of(cls, edit: PromptEditContribution) -> ImpactEdit:
        if edit.replace is not None:
            return cls(target=edit.target, op=EditOp.REPLACE, section=edit.replace, when=edit.when)
        if edit.insert is not None:
            return cls(
                target=edit.target,
                op=EditOp.INSERT,
                section=edit.before or edit.after,
                position=Position.BEFORE if edit.before else Position.AFTER,
                name=edit.insert,
                when=edit.when,
            )
        return cls(target=edit.target, op=EditOp.APPEND, name=edit.append, when=edit.when)


class ImpactTool(BaseModel):
    """A tool it gives the agent, and the four hints igni's permission modes decide on.

    MCP's spelling of the hints, as the manifest has them, because a
    super admin compares the two.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    readOnlyHint: bool  # noqa: N815 — MCP's name for it
    destructiveHint: bool  # noqa: N815
    idempotentHint: bool  # noqa: N815
    openWorldHint: bool  # noqa: N815

    @classmethod
    def of(cls, name: str, hints: ToolHints) -> ImpactTool:
        return cls(name=name, **hints.as_annotations())


class ExtensionImpact(BaseModel):
    """What one extension changes, as a super admin reviews it."""

    model_config = ConfigDict(frozen=True)

    extension_id: str
    name: str
    version: str

    #: Prompts it overrides whole.
    prompt_overrides: list[ImpactOverride] = Field(default_factory=list)
    #: Edits it makes to prompts, in the order it declares them.
    prompt_edits: list[ImpactEdit] = Field(default_factory=list)
    #: ``{{ANCHOR}}`` names it fills.
    anchors: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)

    #: Every tool it gives the agent, with its hints.
    tools: list[ImpactTool] = Field(default_factory=list)
    pages: list[str] = Field(default_factory=list)
    menu: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    settings: list[str] = Field(default_factory=list)
    host: list[str] = Field(default_factory=list)
    rpc: list[str] = Field(default_factory=list)
    egress: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    workspace: str = "none"
    plugin: str | None = None

    @classmethod
    def of(cls, manifest: ExtensionManifest) -> ExtensionImpact:
        contributes = manifest.contributes
        prompt = contributes.prompt
        permissions = manifest.permissions
        return cls(
            extension_id=manifest.id,
            name=manifest.display_name,
            version=manifest.version,
            prompt_overrides=[
                ImpactOverride(
                    target=o.target, sections=list(o.sections), exclusive=o.exclusive, when=o.when
                )
                for o in sorted(prompt.overrides, key=lambda o: o.target)
            ],
            prompt_edits=[ImpactEdit.of(e) for e in prompt.edits],
            anchors=sorted(f.anchor for f in contributes.promptFragments),
            commands=sorted(c.command for c in contributes.commands),
            tools=sorted(
                (ImpactTool.of(tool.name, tool.hints) for tool in permissions.tools),
                key=lambda tool: tool.name,
            ),
            pages=sorted(p.kind for p in contributes.pages),
            menu=sorted(m.command for m in contributes.menu),
            indicators=sorted(i.id for i in contributes.indicators),
            settings=sorted(s.key for s in contributes.settings),
            host=sorted(permissions.host),
            rpc=sorted(permissions.rpc),
            egress=sorted(permissions.egress),
            secrets=sorted(permissions.secrets),
            workspace=permissions.workspace.value,
            plugin=contributes.plugin,
        )

    def claims(self) -> list[str]:
        """Everything this extension wants that only one extension can have."""
        return sorted(
            [o.key for o in self.prompt_overrides]
            + [e.key for e in self.prompt_edits if e.op is EditOp.REPLACE]
            + [Claim.command(c) for c in self.commands]
            + [Claim.anchor(a) for a in self.anchors]
        )

    def items(self) -> list[str]:
        """Everything that can be switched off on its own: the claims, and every insert and append."""
        return sorted(set(self.claims()) | {e.key for e in self.prompt_edits})

    def override(self, target: str) -> ImpactOverride | None:
        return next((o for o in self.prompt_overrides if o.target == target), None)

    def edits_of(self, target: str) -> list[ImpactEdit]:
        return [e for e in self.prompt_edits if e.target == target]


class IgniCommands(BaseModel):
    """igni's own slash commands: claims no extension can have.

    Bare names, as the dispatcher compares them. The live list is the
    dispatcher's; ``docs/igni-commands.json`` is the same list written
    down, for authors and for igni server.
    """

    model_config = ConfigDict(frozen=True)

    commands: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, names: Iterable[str]) -> IgniCommands:
        return cls(commands=sorted({name.lstrip("/").lower() for name in names}))

    @classmethod
    def load(cls, path: Path) -> IgniCommands:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def claims(self) -> list[str]:
        return [Claim.command(f"/{name}") for name in self.commands]

    def clashes(self, impact: ExtensionImpact) -> list[str]:
        """The commands this extension declares that are igni's."""
        own = set(self.commands)
        return [c for c in impact.commands if c.lstrip("/") in own]


class Collision(BaseModel):
    """Two or more extensions wanting one thing."""

    model_config = ConfigDict(frozen=True)

    claim: str
    #: Sorted.
    extensions: list[str]


class Resolution(BaseModel):
    """A super admin's decision on one collision."""

    model_config = ConfigDict(frozen=True)

    claim: str
    #: The extensions it was between, sorted.
    extensions: list[str]
    winner: str
    decided_by: str = ""
    decided_at: datetime | None = None

    def settles(self, collision: Collision) -> bool:
        """Whether this decision covers that collision.

        It does when every extension in the collision was in the decision:
        a decision among three still holds between two of them — on a
        machine that has only those two, or after the third is gone. An
        extension the decision did not consider makes it a new collision.
        """
        return self.claim == collision.claim and set(collision.extensions) <= set(self.extensions)


class SwitchedOff(BaseModel):
    """One thing one extension does not get, and why."""

    model_config = ConfigDict(frozen=True)

    claim: str
    reason: str


class Dropped(SwitchedOff):
    """An edit that lands nowhere, the override that leaves it out, and why."""

    #: The extension whose override has no section the edit names.
    by: str


class ResolvedImpact(BaseModel):
    """The outcome: every collision, the undecided ones, what is off, and what is dropped."""

    collisions: list[Collision] = Field(default_factory=list)
    undecided: list[Collision] = Field(default_factory=list)
    #: ``{extension_id: [what it does not get]}``.
    off: dict[str, list[SwitchedOff]] = Field(default_factory=dict)
    #: ``{extension_id: [edits that land nowhere, and why]}`` — an
    #: insert or a replace aimed at a section another extension's
    #: override does not have. Not decided, not a collision: listed.
    dropped: dict[str, list[Dropped]] = Field(default_factory=dict)

    def allows(self, extension_id: str, claim: str) -> bool:
        return all(entry.claim != claim for entry in self.off.get(extension_id, []))

    def involving(self, extension_id: str) -> list[Collision]:
        """The undecided collisions this extension is in."""
        return [c for c in self.undecided if extension_id in c.extensions]


class ImpactResolver:
    """Finds the collisions among a set of extensions and applies the decisions."""

    def __init__(
        self,
        impacts: Iterable[ExtensionImpact],
        resolutions: Iterable[Resolution] = (),
        *,
        reserved: Iterable[str] = (),
    ) -> None:
        """
        Args:
            reserved: Claims igni keeps for itself — its own commands
                (:meth:`IgniCommands.claims`). An extension claiming
                one loses it, always; there is nothing to decide.
        """
        self._impacts = {impact.extension_id: impact for impact in impacts}
        self._resolutions = list(resolutions)
        self._reserved = set(reserved)

    def enforce(self) -> ResolvedImpact:
        """What the host applies: an undecided claim is off for everyone in it."""
        return self._resolve(enforce=True)

    def survey(self) -> ResolvedImpact:
        """What a super admin is shown: every collision there is to decide."""
        return self._resolve(enforce=False)

    def _resolve(self, *, enforce: bool) -> ResolvedImpact:
        active = {ext: set(impact.items()) for ext, impact in self._impacts.items()}
        outcome = ResolvedImpact()

        for ext in sorted(active):
            for key in sorted(active[ext] & self._reserved):
                self._switch_off(
                    outcome,
                    active,
                    ext,
                    {key},
                    f"{key} is igni's own; igni answers it before any extension could",
                )

        for key in self._keys(active):
            participants = self._participants(key, active)
            if len(participants) < 2:
                continue
            collision = Collision(claim=key, extensions=sorted(participants))
            outcome.collisions.append(collision)
            decision = next((r for r in self._resolutions if r.settles(collision)), None)
            if decision is not None:
                reason = f"{decision.winner} has {key}: decided"
                if decision.decided_by:
                    reason += f" by {decision.decided_by}"
                if decision.decided_at:
                    reason += f" on {decision.decided_at.date().isoformat()}"
                for loser, lost in self._losses(key, decision.winner, participants, active).items():
                    self._switch_off(outcome, active, loser, lost, reason)
                continue
            outcome.undecided.append(collision)
            if enforce:
                for ext in sorted(participants):
                    others = ", ".join(sorted(participants - {ext}))
                    self._switch_off(
                        outcome,
                        active,
                        ext,
                        self._stake(key, ext, active),
                        f"collides with {others} over {key}, and no super admin has "
                        "decided which wins; switched off for all of them until one does",
                    )

        self._drop(outcome, active)
        return outcome

    # ── Who is in it ───────────────────────────────────────────────

    @staticmethod
    def _keys(active: dict[str, set[str]]) -> list[str]:
        """Every claim worth checking: whole prompts first, then the rest by name.

        Whole prompts first because an exclusive override's decision
        settles the edits under it, sections included.
        """
        everything = set().union(*active.values()) if active else set()
        claims = {item for item in everything if "+" not in item.partition(":")[2]}
        wholes = sorted(c for c in claims if Claim.whole_prompt(c) is not None)
        rest = sorted(c for c in claims if Claim.whole_prompt(c) is None)
        return wholes + rest

    def _exclusive(self, ext: str, document: str) -> bool:
        override = self._impacts[ext].override(document)
        return override is not None and override.exclusive

    @staticmethod
    def _editors(document: str, active: dict[str, set[str]]) -> set[str]:
        return {
            ext
            for ext, items in active.items()
            if any(Claim.is_edit(i) and Claim.prompt_document(i) == document for i in items)
        }

    def _participants(self, key: str, active: dict[str, set[str]]) -> set[str]:
        document = Claim.whole_prompt(key)
        holders = {ext for ext, items in active.items() if key in items}
        if document is None or not holders:
            return holders
        # An exclusive override takes on everyone who edits that prompt.
        if any(self._exclusive(ext, document) for ext in holders):
            return holders | (self._editors(document, active) - holders)
        return holders

    def _stake(self, key: str, ext: str, active: dict[str, set[str]]) -> set[str]:
        """What ``ext`` has riding on the collision over ``key``."""
        if key in active[ext]:
            return {key}
        # An editor in an exclusive override's collision: its edits of that prompt.
        document = Claim.whole_prompt(key)
        return {i for i in active[ext] if Claim.is_edit(i) and Claim.prompt_document(i) == document}

    def _losses(
        self, key: str, winner: str, participants: set[str], active: dict[str, set[str]]
    ) -> dict[str, set[str]]:
        """``{loser: what it loses}`` once ``winner`` has ``key``."""
        if winner not in participants:
            # Decided among more extensions than this set has, and the
            # winner is not here: everyone who is lost.
            return {ext: self._stake(key, ext, active) for ext in participants}
        document = Claim.whole_prompt(key)
        if document is None:
            return {ext: {key} for ext in participants - {winner}}
        losses: dict[str, set[str]] = {}
        if key in active[winner]:
            # An override won: the other overrides are off, and if it is
            # exclusive so is everybody's editing of the prompt.
            exclusive = self._exclusive(winner, document)
            for ext in participants - {winner}:
                lost = {key} & active[ext]
                if exclusive:
                    lost |= {
                        i
                        for i in active[ext]
                        if Claim.is_edit(i) and Claim.prompt_document(i) == document
                    }
                losses[ext] = lost
        else:
            # The edits won over an exclusive override: every override
            # in the collision is off.
            for ext in participants - {winner}:
                if key in active[ext]:
                    losses[ext] = {key}
        return losses

    @staticmethod
    def _switch_off(
        outcome: ResolvedImpact,
        active: dict[str, set[str]],
        extension_id: str,
        lost: set[str],
        reason: str,
    ) -> None:
        for item in sorted(lost & active[extension_id]):
            active[extension_id].discard(item)
            outcome.off.setdefault(extension_id, []).append(SwitchedOff(claim=item, reason=reason))

    # ── What lands nowhere ─────────────────────────────────────────

    def _drop(self, outcome: ResolvedImpact, active: dict[str, set[str]]) -> None:
        """Inserts and replacements aimed at sections an override in force does not have."""
        for owner in sorted(active):
            for override in self._impacts[owner].prompt_overrides:
                if override.key not in active[owner]:
                    continue
                while_ = f" while {owner} holds {override.when}" if override.when else ""
                for ext in sorted(active):
                    if ext == owner:
                        continue
                    for edit in self._impacts[ext].edits_of(override.target):
                        if edit.op is EditOp.APPEND or edit.key not in active[ext]:
                            continue
                        if edit.section is not None and not override.has(edit.section):
                            outcome.dropped.setdefault(ext, []).append(
                                Dropped(
                                    claim=edit.key,
                                    by=owner,
                                    reason=f"{owner} overrides {override.target}{while_}, "
                                    f"and its prompt has no section {edit.section!r}",
                                )
                            )


__all__ = [
    "Claim",
    "ClaimKind",
    "Collision",
    "Dropped",
    "ExtensionImpact",
    "IgniCommands",
    "ImpactEdit",
    "ImpactOverride",
    "ImpactResolver",
    "ImpactTool",
    "Resolution",
    "ResolvedImpact",
    "SwitchedOff",
]
