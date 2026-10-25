"""What an extension changes in igni, and where two extensions collide.

An extension can take over a great deal — the whole of the agent's
prompt, if that is what a deployment wants. That is allowed on
purpose, and it makes one question unavoidable: what happens when two
extensions want the same thing? A super admin answers it once, for
every case, by putting the extensions in one **priority** order: the
higher one wins. This module applies that order, and it is the same
answer in two places — the Services page on igni server, where a super
admin sees who wins and who loses before anything ships, and the host,
which applies it when it loads. Nothing is ever held back from
distribution for a collision.

## The impact

:class:`ExtensionImpact` is everything a manifest changes, read off it:
the prompts it overrides and the edits it makes to them, its commands
and anchors, and — for review rather than for collisions — its pages,
menu entries, indicators, settings, tools (each with the four hints
igni's permission modes decide on), host-model grants, hook events, RPC
grants, egress and secrets.

## Prompts: overrides and edits

Per prompt — ``main``, ``plan_mode``, ``agent:<name>`` — an extension
either overrides it whole or edits it: appends a section, inserts one
before or after a named section, replaces a named section. Edits from
several extensions stack, in priority order, and never collide with
each other unless they replace the same section.

An override is the new starting point, not a rival to the edits: the
others' appends land on it, and their inserts and replacements land
where it has the section they name. Where it does not, they are
**dropped** — not a collision — and the drop is listed
(:attr:`ResolvedImpact.dropped`).

## What collides, and who wins

A **claim** is something only one extension can have, spelled
``<kind>:<name>``. In every collision the extension highest in the
priority order (:class:`Priority`) keeps the claim, and every other
loses that claim only — the rest of each loser still works:

* ``prompt:<target>`` — overriding a whole prompt. Of two overrides the
  higher wins. An ``exclusive`` override also takes on every extension
  that edits that prompt, and holds only if it is above all of them:
  then their edits of that prompt are off. If any of them is higher,
  the exclusive override is off and the edits stand (the next override
  down, if there is one, has the prompt). One rule — the higher wins
  each battle — applied to the battles an exclusive override starts;
* ``prompt:<target>#<section>`` — replacing one section;
* ``command:/<name>`` — a slash command;
* ``anchor:<NAME>`` — the text at a ``{{NAME}}`` in a prompt.

An insert or an append is spelled ``prompt:<target>+<name>`` where an
item has to be named — switched off, dropped — and is never a claim.

**igni's own commands** are not anybody's to claim. An extension
declaring ``/clear`` collides with igni, not with another extension:
igni's command runs first and the extension's never would. igni server
refuses such a bundle at upload; the host, which may be a newer igni
with a command the bundle did not know about, switches the claim off
(:class:`IgniCommands`, passed to :class:`ImpactResolver` as
``reserved``). ``docs/igni-commands.json`` lists them for authors and
for igni server, and a test holds it to the dispatcher.

**Not collisions**, because they cannot be: inserts and appends stack;
hooks (``permissions.hooks``) chain in priority order — the first
reject ends the chain, and each change is what the next one is handed; tools are registered under the extension's server name; pages
and indicators are named in the extension's own namespace; settings
are stored per extension; a plugin's own agents load under the
extension's id; grants and egress only add. A condition (``when``)
does not avoid a collision either — two extensions that would replace
one section under different conditions still claim it.

Every loss says why, beginning ``lower priority than <extension>``.

``tests/fixtures/extension_impact_cases.json`` holds cases igni server
runs too: two implementations of one rule, held to the same answers.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.hooks.extension_gate import DECIDING
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


class ImpactHook(BaseModel):
    """A hook event it answers, and whether its answer decides it or it is only told."""

    model_config = ConfigDict(frozen=True)

    event: str
    #: ``True`` for an event whose answer counts (``DECIDING``): it may
    #: stop the event, or change it; ``False`` for one it is only told.
    decides: bool = False


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
    #: igni's hook events it answers. Never a claim: hooks chain in
    #: priority order.
    hooks: list[ImpactHook] = Field(default_factory=list)
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
            hooks=sorted(
                (
                    ImpactHook(event=event.value, decides=event in DECIDING)
                    for event in permissions.hooks
                ),
                key=lambda hook: hook.event,
            ),
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
    """Two or more extensions wanting one thing, and which of them has it."""

    model_config = ConfigDict(frozen=True)

    claim: str
    #: Sorted.
    extensions: list[str]
    #: The extension that keeps the claim: the highest in the priority
    #: order. ``None`` only for an exclusive override that a higher
    #: extension's edits switched off, with no other override left —
    #: then nobody holds the whole prompt and the edits stand.
    winner: str | None = None
    #: Sorted: the extensions that lost something in it, each by being
    #: lower in priority than another in it.
    losers: list[str] = Field(default_factory=list)


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
    """The outcome: every collision, what is off, and what is dropped."""

    collisions: list[Collision] = Field(default_factory=list)
    #: ``{extension_id: [what it does not get]}``.
    off: dict[str, list[SwitchedOff]] = Field(default_factory=dict)
    #: ``{extension_id: [edits that land nowhere, and why]}`` — an
    #: insert or a replace aimed at a section another extension's
    #: override does not have. Not a collision: listed.
    dropped: dict[str, list[Dropped]] = Field(default_factory=dict)

    def allows(self, extension_id: str, claim: str) -> bool:
        return all(entry.claim != claim for entry in self.off.get(extension_id, []))

    def involving(self, extension_id: str) -> list[Collision]:
        """The collisions this extension is in."""
        return [c for c in self.collisions if extension_id in c.extensions]


class Priority:
    """The super admin's one order of extensions: first is highest.

    An extension the order does not name ranks below every one it does,
    by id — igni server always sends the whole order, so that is only
    a machine with a development install.
    """

    def __init__(self, order: Iterable[str] = ()) -> None:
        self._rank = {extension_id: index for index, extension_id in enumerate(order)}

    def key(self, extension_id: str) -> tuple[int, str]:
        return (self._rank.get(extension_id, len(self._rank)), extension_id)

    def ordered(self, extension_ids: Iterable[str]) -> list[str]:
        return sorted(extension_ids, key=self.key)

    def above(self, first: str, second: str) -> bool:
        return self.key(first) < self.key(second)


def lower_than(winner: str) -> str:
    """How every loss by priority begins: ``lower priority than <extension>``."""
    return f"lower priority than {winner}"


class ImpactResolver:
    """Finds the collisions among a set of extensions and settles each by priority."""

    def __init__(
        self,
        impacts: Iterable[ExtensionImpact],
        priority: Iterable[str] = (),
        *,
        reserved: Iterable[str] = (),
    ) -> None:
        """
        Args:
            priority: The super admin's order of extension ids, highest first.
            reserved: Claims igni keeps for itself — its own commands
                (:meth:`IgniCommands.claims`). An extension claiming
                one loses it, always.
        """
        self._impacts = {impact.extension_id: impact for impact in impacts}
        self._priority = Priority(priority)
        self._reserved = set(reserved)

    def resolve(self) -> ResolvedImpact:
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
            document = Claim.whole_prompt(key)
            if document is not None:
                self._settle_prompt(key, document, active, outcome)
                continue
            holders = self._priority.ordered(ext for ext, items in active.items() if key in items)
            if len(holders) < 2:
                continue
            winner = holders[0]
            outcome.collisions.append(
                Collision(
                    claim=key, extensions=sorted(holders), winner=winner, losers=sorted(holders[1:])
                )
            )
            for loser in holders[1:]:
                self._switch_off(
                    outcome, active, loser, {key}, f"{lower_than(winner)}, which also claims {key}"
                )

        self._drop(outcome, active)
        return outcome

    def _settle_prompt(
        self, key: str, document: str, active: dict[str, set[str]], outcome: ResolvedImpact
    ) -> None:
        """Overrides of one prompt, and an exclusive one against the edits.

        An exclusive override holds only above every extension editing
        its prompt: priority is "the higher one wins every battle", and
        an exclusive override battles each of them. Outranked, it is off
        and the edits stand. Of the overrides left, the highest has the
        prompt; if it is exclusive, everybody else's edits of it are off.
        """
        overriders = self._priority.ordered(ext for ext, items in active.items() if key in items)
        if not overriders:
            return
        editors = self._priority.ordered(
            ext for ext in active if ext not in overriders and self._edits_of(ext, document, active)
        )
        exclusive = [ext for ext in overriders if self._exclusive(ext, document)]
        participants = set(overriders) | (set(editors) if exclusive else set())
        if len(participants) < 2:
            return

        standing: list[str] = []
        losers: set[str] = set()
        for ext in overriders:
            higher = [e for e in editors if self._priority.above(e, ext)]
            if self._exclusive(ext, document) and higher:
                self._switch_off(
                    outcome,
                    active,
                    ext,
                    {key},
                    f"{lower_than(higher[0])}, which edits {document}; an exclusive override "
                    "holds only above every extension editing its prompt",
                )
                losers.add(ext)
                continue
            standing.append(ext)

        winner = standing[0] if standing else None
        for ext in standing[1:]:
            losers.add(ext)
            self._switch_off(
                outcome,
                active,
                ext,
                {key},
                f"{lower_than(winner)}, which also overrides {document}",
            )
        if winner is not None and self._exclusive(winner, document):
            for ext in editors:
                losers.add(ext)
                self._switch_off(
                    outcome,
                    active,
                    ext,
                    self._edits_of(ext, document, active),
                    f"{lower_than(winner)}, whose override of {document} is exclusive",
                )
        outcome.collisions.append(
            Collision(
                claim=key, extensions=sorted(participants), winner=winner, losers=sorted(losers)
            )
        )

    # ── Who is in it ───────────────────────────────────────────────

    @staticmethod
    def _keys(active: dict[str, set[str]]) -> list[str]:
        """Every claim worth checking: whole prompts first, then the rest by name.

        Whole prompts first because an exclusive override that holds
        switches the edits under it off, section replacements included.
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
    def _edits_of(ext: str, document: str, active: dict[str, set[str]]) -> set[str]:
        return {i for i in active[ext] if Claim.is_edit(i) and Claim.prompt_document(i) == document}

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
    "ImpactHook",
    "ImpactOverride",
    "ImpactResolver",
    "ImpactTool",
    "Priority",
    "ResolvedImpact",
    "SwitchedOff",
    "lower_than",
]
