"""The organisation's say over which permission modes a session may enter.

Two things an organisation can set, and only an organisation — the
managed-settings file or the group policy, never a user's or a
project's config:

* **A floor.** ``permissions.mode`` set by either tier is the loosest
  mode the session may run in; with both set, the stricter holds. ``/accept``, ``/bypass``, the HITL
  "accept all edits" shortcut and ``exit_plan_mode`` approval all go
  through :meth:`ModePolicy.refusal`, so none of them can move past it.
  A stricter mode is always reachable.
* **``permissions.allow_bypass: false``.** ``bypassPermissions`` cannot
  be entered at all (either tier saying so is enough) — not by ``--auto-approve`` (the loader clamps the
  starting mode), not by ``/bypass``, not by a tool.

With neither set — the case for anyone not under an organisation's
policy — every mode is reachable, ``bypassPermissions`` included.

The ranking
-----------

From strictest to loosest::

    plan, dontAsk  <  default  <  acceptEdits  <  bypassPermissions

``plan`` and ``dontAsk`` share the bottom rank and are deliberately not
ordered against each other: ``plan`` runs read-only shell and web reads
that ``dontAsk`` would deny, and ``dontAsk`` runs anything its allow
rules name, edits included, which ``plan`` never does. Neither is a
safe substitute for the other, so a floor of one does not admit the
other. Every other move strictly down the ranking is allowed.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ember_code.core.config.permission_eval.schemas import PermissionMode

#: Strictest first. Equal ranks are not interchangeable — see the module
#: docstring.
MODE_RANK: dict[PermissionMode, int] = {
    PermissionMode.PLAN: 0,
    PermissionMode.DONT_ASK: 0,
    PermissionMode.DEFAULT: 1,
    PermissionMode.ACCEPT_EDITS: 2,
    PermissionMode.BYPASS_PERMISSIONS: 3,
}


def is_at_least_as_strict(candidate: PermissionMode, floor: PermissionMode) -> bool:
    """May a session whose floor is ``floor`` run in ``candidate``?"""
    if candidate is floor:
        return True
    return MODE_RANK[candidate] < MODE_RANK[floor]


class ModeFloor(BaseModel):
    """One tier's ``permissions.mode``: the loosest mode it allows."""

    model_config = ConfigDict(frozen=True)

    mode: PermissionMode
    source: str


class ModePolicy(BaseModel):
    """What the managed settings and group policy say about modes.

    ``floors`` holds each organisation tier that set ``permissions.mode``,
    strictest first; a mode must satisfy every one of them, so the
    stricter of managed and group always wins. ``allow_bypass`` is
    ``False`` if either tier says so. The sources name the tier, for the
    refusal message.
    """

    model_config = ConfigDict(frozen=True)

    MANAGED: ClassVar[str] = "managed settings"
    GROUP: ClassVar[str] = "group policy"

    floors: tuple[ModeFloor, ...] = ()
    allow_bypass: bool = True
    bypass_source: str | None = None

    @classmethod
    def from_tiers(
        cls,
        managed: dict[str, Any] | None,
        group: dict[str, Any] | None,
    ) -> ModePolicy:
        """Build from the raw ``managed`` and ``group`` settings dicts.

        Not the merge's precedence: for a limit the stricter tier wins,
        whichever it is. A mode that does not parse sets no floor, and an
        ``allow_bypass`` that is not a boolean is ignored.
        """
        floors: list[ModeFloor] = []
        bypass_sources: list[str] = []
        for source, payload in ((cls.MANAGED, managed), (cls.GROUP, group)):
            perms = (payload or {}).get("permissions")
            if not isinstance(perms, dict):
                continue
            raw_mode = perms.get("mode")
            if isinstance(raw_mode, str) and raw_mode in PermissionMode._value2member_map_:
                floors.append(ModeFloor(mode=PermissionMode(raw_mode), source=source))
            if perms.get("allow_bypass") is False:
                bypass_sources.append(source)
        floors.sort(key=lambda f: MODE_RANK[f.mode])
        return cls(
            floors=tuple(floors),
            allow_bypass=not bypass_sources,
            bypass_source=" and ".join(bypass_sources) or None,
        )

    @property
    def floor(self) -> PermissionMode | None:
        """The strictest floor, or ``None`` when no tier set a mode."""
        return self.floors[0].mode if self.floors else None

    @property
    def floor_source(self) -> str | None:
        return self.floors[0].source if self.floors else None

    @property
    def is_empty(self) -> bool:
        return not self.floors and self.allow_bypass

    def refusal(self, mode: PermissionMode) -> str | None:
        """Why ``mode`` may not be entered, or ``None`` if it may."""
        if mode is PermissionMode.BYPASS_PERMISSIONS and not self.allow_bypass:
            return (
                f"Error: bypassPermissions is turned off by your organisation's "
                f"{self.bypass_source or 'policy'} (permissions.allow_bypass: false)."
            )
        for floor in self.floors:
            if not is_at_least_as_strict(mode, floor.mode):
                return (
                    f"Error: {mode.value} is looser than the {floor.mode.value} mode set by "
                    f"your organisation's {floor.source}; this session cannot leave "
                    f"{floor.mode.value} for a looser mode."
                )
        return None

    def clamp_start(self, mode: PermissionMode) -> PermissionMode:
        """The mode a session starts in, given that it asked for ``mode``.

        A mode the policy refuses becomes the strictest floor the policy
        itself accepts, or ``default``, or — if even ``default`` is
        refused — the strictest floor regardless.
        """
        if self.refusal(mode) is None:
            return mode
        for floor in self.floors:
            if self.refusal(floor.mode) is None:
                return floor.mode
        if self.refusal(PermissionMode.DEFAULT) is None:
            return PermissionMode.DEFAULT
        return self.floors[0].mode if self.floors else PermissionMode.DEFAULT


__all__ = ["MODE_RANK", "ModeFloor", "ModePolicy", "is_at_least_as_strict"]
