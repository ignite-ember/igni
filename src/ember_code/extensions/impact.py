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
the prompt it replaces and the slots it adds to, the agents it stands
in for, its commands and anchors, and — for review rather than for
collisions — its pages, menu entries, indicators, settings, tools (each
with the four hints igni's permission modes decide on), host-model
grants, RPC grants, egress and secrets.

## What collides

A **claim** is something only one extension can have. There are four
kinds, each spelled ``<kind>:<name>``:

* ``prompt:<document>`` and ``prompt:<document>.<section>`` — replacing
  a whole prompt, or one of its sections;
* ``command:/<name>`` — a slash command;
* ``agent:<name>`` — standing in for one of igni's agents;
* ``anchor:<NAME>`` — the text at a ``{{NAME}}`` in an agent's prompt.

Two extensions collide when they claim the same thing, and also when
one replaces a whole prompt and the other replaces any section of it:
a section of a prompt someone else wrote does not exist. Two
extensions replacing *different* sections of one prompt do not collide.

**igni's own commands** are not anybody's to claim. An extension
declaring ``/clear`` does not collide with another extension, it
collides with igni, and there is nothing for a super admin to decide:
igni's command runs first and the extension's never would. igni server
refuses such a bundle at upload; the host, which may be a newer igni
with a command the bundle did not know about, switches the claim off
(:class:`IgniCommands`, passed to :class:`ImpactResolver` as
``reserved``). ``docs/igni-commands.json`` lists them for authors and
for igni server, and a test holds it to the dispatcher.

**Not collisions**, because they cannot be: slot fills stack, each
under its extension's name; tools are registered under the
extension's server name; pages and indicators are named in the
extension's own namespace; settings are stored per extension; a
plugin's own agents load under the extension's id; grants and egress
only add. A condition (``when``) does not avoid a collision either —
two extensions that would replace one section under different
conditions still claim it.

## Deciding

A super admin picks the winner of each collision on igni server. A
:class:`Resolution` names the claim, the extensions it was between,
and the winner, with who decided and when. It applies while the same
extensions are in the collision; a third extension joining it is a new
collision. The losers' claim is switched off — the rest of each loser
still works.

Collisions are evaluated whole-prompt first, then everything else in
name order, so a decision on a whole prompt settles the sections under
it: if the extension replacing ``main`` wins, every other extension's
``main`` sections are off and no section collision is left to decide.

**Undecided**, the host switches the claim off for every extension in
the collision (:meth:`ImpactResolver.enforce`), and says why. Listing
for a super admin keeps undecided claims in play
(:meth:`ImpactResolver.survey`), so every collision there is to decide
is shown at once.

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
from ember_code.extensions.manifest import ExtensionManifest


class ClaimKind(StrEnum):
    PROMPT = "prompt"
    COMMAND = "command"
    AGENT = "agent"
    ANCHOR = "anchor"


class Claim:
    """Spelling and reading claim names. One place, because igni server reads them too."""

    @staticmethod
    def prompt(target: str) -> str:
        return f"{ClaimKind.PROMPT.value}:{target}"

    @staticmethod
    def command(command: str) -> str:
        return f"{ClaimKind.COMMAND.value}:{command}"

    @staticmethod
    def agent(name: str) -> str:
        return f"{ClaimKind.AGENT.value}:{name}"

    @staticmethod
    def anchor(name: str) -> str:
        return f"{ClaimKind.ANCHOR.value}:{name}"

    @staticmethod
    def whole_prompt(claim: str) -> str | None:
        """``main`` for ``prompt:main``; ``None`` for anything else."""
        kind, _, name = claim.partition(":")
        return name if kind == ClaimKind.PROMPT.value and "." not in name else None

    @staticmethod
    def prompt_document(claim: str) -> str | None:
        """``main`` for ``prompt:main`` and ``prompt:main.workflow``."""
        kind, _, name = claim.partition(":")
        return name.partition(".")[0] if kind == ClaimKind.PROMPT.value else None


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

    #: Prompt targets it replaces: ``main``, ``main.workflow``.
    prompt_replaces: list[str] = Field(default_factory=list)
    #: Slots it adds to: ``main.code_intelligence``.
    prompt_slots: list[str] = Field(default_factory=list)
    #: igni's agents it stands in for.
    agents: list[str] = Field(default_factory=list)
    #: ``{{ANCHOR}}`` names it fills.
    anchors: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    #: ``{claim or slot claim: condition}`` for what holds only under one.
    conditions: dict[str, str] = Field(default_factory=dict)

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
        conditions: dict[str, str] = {}
        for replacement in prompt.replace:
            if replacement.when:
                conditions[Claim.prompt(replacement.target)] = replacement.when
        for fill in prompt.slots:
            if fill.when:
                conditions[f"slot:{fill.slot}"] = fill.when
        for agent in prompt.agents:
            if agent.when:
                conditions[Claim.agent(agent.agent)] = agent.when
        return cls(
            extension_id=manifest.id,
            name=manifest.display_name,
            version=manifest.version,
            prompt_replaces=sorted(r.target for r in prompt.replace),
            prompt_slots=sorted(s.slot for s in prompt.slots),
            agents=sorted(a.agent for a in prompt.agents),
            anchors=sorted(f.anchor for f in contributes.promptFragments),
            commands=sorted(c.command for c in contributes.commands),
            conditions=conditions,
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
            [Claim.prompt(t) for t in self.prompt_replaces]
            + [Claim.command(c) for c in self.commands]
            + [Claim.agent(a) for a in self.agents]
            + [Claim.anchor(a) for a in self.anchors]
        )


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
    """One claim one extension does not get, and why."""

    model_config = ConfigDict(frozen=True)

    claim: str
    reason: str


class ResolvedImpact(BaseModel):
    """The outcome: every collision, the undecided ones, and what is off."""

    collisions: list[Collision] = Field(default_factory=list)
    undecided: list[Collision] = Field(default_factory=list)
    #: ``{extension_id: [what it does not get]}``.
    off: dict[str, list[SwitchedOff]] = Field(default_factory=dict)

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
        active = {ext: set(impact.claims()) for ext, impact in self._impacts.items()}
        outcome = ResolvedImpact()

        for ext in sorted(active):
            for key in sorted(active[ext] & self._reserved):
                self._switch_off(
                    outcome,
                    active,
                    ext,
                    key,
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
                # The winner may not be here — decided among more
                # extensions than this machine has. Then everyone here lost.
                losers = (
                    self._losers(key, decision.winner, participants, active)
                    if decision.winner in participants
                    else participants
                )
                for loser in losers:
                    self._switch_off(
                        outcome,
                        active,
                        loser,
                        key,
                        f"{decision.winner} has {key}: decided"
                        + (f" by {decision.decided_by}" if decision.decided_by else "")
                        + (
                            f" on {decision.decided_at.date().isoformat()}"
                            if decision.decided_at
                            else ""
                        ),
                    )
                continue
            outcome.undecided.append(collision)
            if enforce:
                for ext in participants:
                    others = ", ".join(sorted(participants - {ext}))
                    self._switch_off(
                        outcome,
                        active,
                        ext,
                        key,
                        f"collides with {others} over {key}, and no super admin has "
                        "decided which wins; switched off for all of them until one does",
                    )
        return outcome

    @staticmethod
    def _keys(active: dict[str, set[str]]) -> list[str]:
        """Every claim worth checking: whole prompts first, then the rest by name."""
        everything = set().union(*active.values()) if active else set()
        wholes = sorted(c for c in everything if Claim.whole_prompt(c) is not None)
        rest = sorted(c for c in everything if Claim.whole_prompt(c) is None)
        return wholes + rest

    @staticmethod
    def _participants(key: str, active: dict[str, set[str]]) -> set[str]:
        document = Claim.whole_prompt(key)
        if document is None:
            return {ext for ext, claims in active.items() if key in claims}
        # A whole prompt: everyone replacing it, and — when anyone is —
        # everyone replacing any section of it.
        whole = {ext for ext, claims in active.items() if key in claims}
        if not whole:
            return set()
        sections = {
            ext
            for ext, claims in active.items()
            if any(Claim.prompt_document(c) == document for c in claims)
        }
        return whole | sections

    @staticmethod
    def _losers(
        key: str, winner: str, participants: set[str], active: dict[str, set[str]]
    ) -> set[str]:
        if Claim.whole_prompt(key) is None or key in active[winner]:
            return participants - {winner}
        # The winner replaces sections only: it conflicts with the
        # extensions replacing the whole prompt, and not with the others
        # replacing other sections — those meet again, section by section.
        return {ext for ext in participants - {winner} if key in active[ext]}

    @staticmethod
    def _switch_off(
        outcome: ResolvedImpact,
        active: dict[str, set[str]],
        extension_id: str,
        key: str,
        reason: str,
    ) -> None:
        document = Claim.whole_prompt(key)
        if document is None:
            lost = {key} & active[extension_id]
        else:
            # Losing a whole prompt loses every part of it: a section of
            # a prompt somebody else wrote does not exist.
            lost = {c for c in active[extension_id] if Claim.prompt_document(c) == document}
        for claim in sorted(lost):
            active[extension_id].discard(claim)
            outcome.off.setdefault(extension_id, []).append(SwitchedOff(claim=claim, reason=reason))


__all__ = [
    "Claim",
    "ClaimKind",
    "Collision",
    "ExtensionImpact",
    "IgniCommands",
    "ImpactResolver",
    "Resolution",
    "ResolvedImpact",
    "SwitchedOff",
]
