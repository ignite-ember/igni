"""Wire schemas for the panel RPCs.

Companion module to :mod:`server_panels` — follows the sibling
``schemas_*.py`` convention (see :mod:`schemas_run`,
:mod:`schemas_hitl`, etc.) so every RPC response served by the
panel controllers is a typed Pydantic model rather than a raw
dict.

Contains:

* :class:`OutputStyleInfo` / :class:`OutputStylesResult` — moved
  verbatim from :mod:`server_panels` so the controller file drops
  to a thin composition facade.
* :class:`HookEntryView` — typed replacement for the ``list[dict]``
  the hooks panel used to return. Its :meth:`HookEntryView.from_hook`
  factory concentrates the (heterogeneous) hook-object reach-ins
  in one place.
* :class:`BuiltinSlashCommand` / :class:`MarkdownSlashCommand` /
  :class:`SkillSlashCommand` + the discriminated
  :data:`SlashCommandEntry` alias — typed replacement for the
  ``list[dict]`` the slash-command catalog used to return, one
  variant per source with dedicated ``from_*`` factories.
* :class:`PromoteEphemeralResult` / :class:`DiscardEphemeralResult`
  — typed results for the promote/discard mutators, replacing the
  overloaded ``msg.Info`` (which conflates success and failure).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ember_code.core.hooks.schemas import HookDefinition
    from ember_code.core.skills.parser import SkillEntry
    from ember_code.core.utils.markdown_commands import MarkdownCommand


class OutputStyleInfo(BaseModel):
    """One output-style entry in :attr:`OutputStylesResult.styles`."""

    name: str
    description: str


class OutputStylesResult(BaseModel):
    """Wire shape for :meth:`OutputStylesCatalog.snapshot` —
    discovered styles + the currently-applied one for the picker
    chip."""

    active: str
    styles: list[OutputStyleInfo]


class HookEntryView(BaseModel):
    """Flat, wire-friendly view of one hook for the hooks panel.

    The heterogeneous ``session.hooks_map`` may hold command hooks,
    HTTP hooks, MCP-tool hooks etc. — this projection collapses the
    union into a single dict-shaped record. Fields not applicable
    to a given hook type default to empty (``command`` on an HTTP
    hook, ``url`` on a command hook, etc.).
    """

    event: str
    type: str = ""
    command: str = ""
    url: str = ""
    matcher: str = ""
    timeout_ms: int = 0
    background: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_hook(cls, hook: HookDefinition, event: object) -> HookEntryView:
        """Project a :class:`HookDefinition` into the panel view.

        Uses ``getattr`` defensively — the panel is used against
        older sessions whose hook objects predate newer optional
        fields (``background``, structured ``headers``). The
        defensive reads live here so the callsite in
        :class:`HooksPanelController` stays clean.
        """
        return cls(
            event=str(event),
            type=getattr(hook, "type", "") or "",
            command=getattr(hook, "command", "") or "",
            url=getattr(hook, "url", "") or "",
            matcher=getattr(hook, "matcher", "") or "",
            timeout_ms=int(getattr(hook, "timeout", 0) or 0),
            background=bool(getattr(hook, "background", False)),
            headers=dict(getattr(hook, "headers", {}) or {}),
        )


class BuiltinSlashCommand(BaseModel):
    """A built-in slash command exposed to completion UIs."""

    source: Literal["builtin"] = "builtin"
    name: str
    description: str = ""
    argument_hint: str = ""

    @classmethod
    def from_builtin(cls, name: str, description: str) -> BuiltinSlashCommand:
        return cls(name=name, description=description)


class MarkdownSlashCommand(BaseModel):
    """A user-authored ``.md`` slash command (CC-parity file drop)."""

    source: Literal["markdown"] = "markdown"
    name: str
    description: str = ""
    argument_hint: str = ""

    @classmethod
    def from_markdown(cls, md: MarkdownCommand) -> MarkdownSlashCommand:
        return cls(
            name=md.name,
            description=md.description,
            argument_hint=md.argument_hint,
        )


class SkillSlashCommand(BaseModel):
    """A user-invocable skill exposed as a slash command."""

    source: Literal["skill"] = "skill"
    name: str
    description: str = ""
    argument_hint: str = ""

    @classmethod
    def from_skill(cls, skill: SkillEntry) -> SkillSlashCommand:
        return cls(
            name=skill.name,
            description=skill.description,
            argument_hint=getattr(skill, "argument_hint", ""),
        )


SlashCommandEntry = Annotated[
    BuiltinSlashCommand | MarkdownSlashCommand | SkillSlashCommand,
    Field(discriminator="source"),
]
"""Discriminated union — the ``source`` string picks the variant.

Kept as a bare-string discriminator (``'builtin'|'markdown'|'skill'``)
rather than an Enum so IDE-plugin JSON consumers see plain string
literals in the wire payload.
"""


class PromoteEphemeralResult(BaseModel):
    """Typed result for :meth:`AgentsPanelController.promote`.

    Replaces the stringly-typed ``msg.Info(text=...)`` return that
    used to conflate success and failure. ``ok=False`` +
    ``reason=...`` carries the caught exception message; ``ok=True``
    + ``dest=...`` carries the persisted file path.
    """

    ok: bool
    name: str
    dest: str = ""
    reason: str = ""


class DiscardEphemeralResult(BaseModel):
    """Typed result for :meth:`AgentsPanelController.discard`."""

    ok: bool
    name: str
    reason: str = ""


__all__ = [
    "OutputStyleInfo",
    "OutputStylesResult",
    "HookEntryView",
    "BuiltinSlashCommand",
    "MarkdownSlashCommand",
    "SkillSlashCommand",
    "SlashCommandEntry",
    "PromoteEphemeralResult",
    "DiscardEphemeralResult",
]
