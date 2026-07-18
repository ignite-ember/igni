"""Session mode-state mutators.

Small helpers extracted from :mod:`ember_code.core.session.core`
that flip runtime state without rebuilding the agent:

* :meth:`RuntimeModeCoordinator.set_output_style` — hot-patches
  the main team's ``instructions`` list to swap the ``# Output
  style: ...`` block. The next ``arun`` sees the new tone; no
  team rebuild needed.
* :meth:`RuntimeModeCoordinator.set_permission_mode` — flips the
  live ``PermissionEvaluator`` mode. The evaluator instance is
  shared with the tool-event hook, so the change takes effect
  on the very next tool call.

Both broadcast the corresponding change event to attached
clients (``output_style_changed`` / ``permission_mode_changed``)
so the FE's status chip updates without polling. Broadcast
payloads use the typed :class:`OutputStyleChangedBroadcast` /
:class:`PermissionModeChangedBroadcast` schemas (Rule 1 /
Pattern 2), sourced from the canonical
:mod:`~ember_code.core.session.schemas` module.

The ``# Output style:`` block encoding — the marker prefix and
the empty-body-prune rule — is owned by
:class:`OutputStyleInstructionsPatcher` in this same module.
Callers cross the class boundary; the coordinator composes the
patcher rather than reaching into the team's ``instructions``
list itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ember_code.core.config.permission_eval import PermissionMode
from ember_code.core.session.broadcast_schema import BroadcastEvent
from ember_code.core.session.schemas import (
    OutputStyleChangedBroadcast,
    PermissionModeChangedBroadcast,
)

if TYPE_CHECKING:
    from agno.agent import Agent

    from ember_code.core.session.core import Session


class OutputStyleInstructionsPatcher:
    """Owns the ``# Output style: <name>`` block encoding inside a
    team's ``instructions`` list.

    Stateless — one instance per :class:`RuntimeModeCoordinator`
    (composed in ``__init__``). Concentrates the marker prefix
    (``_MARKER_PREFIX``) and the empty-body-prune rule so there
    is exactly one place in the codebase that knows how the
    block is spelled. A future reader (e.g. a status pane that
    shows the active-style body) imports from here.
    """

    _MARKER_PREFIX: ClassVar[str] = "# Output style:"

    def apply(self, team: Agent | None, style_name: str, body: str) -> None:
        """Replace any existing style block on ``team.instructions``
        with the new one.

        Behaviour:

        * ``team is None`` is a no-op — the style change takes
          effect on the next team build.
        * The existing block (zero or one — only one active style
          at a time) is pruned.
        * If ``body`` is empty, no new block is appended (the
          "silent" style should remove the header rather than
          leave a bare marker line).
        * Otherwise a fresh ``# Output style: <name>\\n\\n<body>``
          block is appended.
        """
        if team is None:
            return
        insts = team.instructions
        if not isinstance(insts, list):
            return
        pruned = [
            s for s in insts if not (isinstance(s, str) and s.startswith(self._MARKER_PREFIX))
        ]
        if body:
            pruned.append(f"{self._MARKER_PREFIX} {style_name}\n\n{body}")
        team.instructions = pruned


class RuntimeModeCoordinator:
    """Owns runtime-state mutations that don't require an agent rebuild.

    Constructor holds a reference to the session so the coordinator
    can reach ``main_team`` (which is reassigned by compact /
    plugin-reload / MCP-rebuild — reading through ``self._session``
    guarantees the live instance).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._patcher = OutputStyleInstructionsPatcher()

    def set_output_style(self, name: str) -> str:
        """Switch the active output style at runtime.

        Returns a short status string for the slash command to echo.
        Unknown style names produce a list of available options.
        """
        session = self._session
        clean = (name or "").strip()
        if clean not in session.output_styles:
            available = sorted(session.output_styles)
            return (
                f"Error: unknown output style {clean!r}. "
                f"Available: {', '.join(available) if available else '(none configured)'}"
            )
        prev = session.active_output_style
        session.set_active_output_style(clean)
        self._patcher.apply(
            session.main_team,
            clean,
            session.output_styles[clean].body,
        )
        # Broadcast so the FE can show the active style in a status
        # chip (parallel to permission_mode_changed).
        session.broadcast_bus.emit(
            BroadcastEvent(
                channel="output_style_changed",
                payload=OutputStyleChangedBroadcast(style=clean, previous=prev).model_dump(),
            )
        )
        if prev == clean:
            return f"Output style already {clean}."
        return f"Output style: {prev or '(none)'} → {clean}."

    def set_permission_mode(self, mode: str) -> str:
        """Flip the live ``PermissionEvaluator`` mode (e.g. into or
        out of plan mode) without rebuilding the agent.

        Broadcasts a ``permission_mode_changed`` push so the FE's
        mode badge updates without polling. Returns a short status
        string for the caller (slash command, ``exit_plan_mode``
        tool) to surface.
        """
        session = self._session
        try:
            new_mode = PermissionMode(mode)
        except ValueError:
            valid = ", ".join(m.value for m in PermissionMode)
            return f"Error: unknown permission mode {mode!r}. Valid: {valid}"
        prev = session.permission_evaluator.mode
        session.permission_evaluator.mode = new_mode
        if prev == new_mode:
            return f"Permission mode already {new_mode.value}."
        # ``mode="json"`` collapses the :class:`PermissionMode`
        # enum fields to their ``.value`` strings so downstream
        # broadcast consumers keep the historic wire shape.
        session.broadcast_bus.emit(
            BroadcastEvent(
                channel="permission_mode_changed",
                payload=PermissionModeChangedBroadcast(mode=new_mode, previous=prev).model_dump(
                    mode="json"
                ),
            )
        )
        return f"Permission mode: {prev.value} → {new_mode.value}."


__all__ = [
    "OutputStyleInstructionsPatcher",
    "RuntimeModeCoordinator",
]
