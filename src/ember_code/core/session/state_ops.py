"""Session mode-state mutators.

Small helpers extracted from :mod:`ember_code.core.session.core`
that flip runtime state without rebuilding the agent:

* :func:`set_output_style` — hot-patches the main team's
  ``instructions`` list to swap the ``# Output style: ...``
  block. The next ``arun`` sees the new tone; no team rebuild
  needed.
* :func:`set_permission_mode` — flips the live
  ``PermissionEvaluator`` mode. The evaluator instance is
  shared with the tool-event hook, so the change takes effect
  on the very next tool call.

Both broadcast the corresponding change event to attached
clients (``output_style_changed`` / ``permission_mode_changed``)
so the FE's status chip updates without polling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


def set_output_style(session: "Session", name: str) -> str:
    """Switch the active output style at runtime.

    Returns a short status string for the slash command to echo.
    Unknown style names produce a list of available options.
    """
    clean = (name or "").strip()
    if clean not in session.output_styles:
        available = sorted(session.output_styles)
        return (
            f"Error: unknown output style {clean!r}. "
            f"Available: {', '.join(available) if available else '(none configured)'}"
        )
    prev = session._active_output_style
    session._active_output_style = clean
    # Patch the live team's instructions so the next ``arun``
    # picks up the new style body. We look for the existing
    # ``# Output style:`` block (zero or one — only one active
    # style at a time) and replace it. The team may not exist
    # yet on a partially-initialised session (tests via
    # ``__new__``); fall through silently in that case — the
    # change takes effect on first build.
    team = getattr(session, "main_team", None)
    if team is not None and hasattr(team, "instructions"):
        new_block = (
            f"# Output style: {clean}\n\n{session.output_styles[clean].body}"
            if session.output_styles[clean].body
            else ""
        )
        insts = team.instructions
        if isinstance(insts, list):
            # Strip any existing style block.
            pruned = [
                s for s in insts if not (isinstance(s, str) and s.startswith("# Output style:"))
            ]
            if new_block:
                pruned.append(new_block)
            team.instructions = pruned
    # Broadcast so the FE can show the active style in a status
    # chip (parallel to permission_mode_changed).
    session.broadcast(
        "output_style_changed",
        {"style": clean, "previous": prev},
    )
    if prev == clean:
        return f"Output style already {clean}."
    return f"Output style: {prev or '(none)'} → {clean}."


def set_permission_mode(session: "Session", mode: str) -> str:
    """Flip the live ``PermissionEvaluator`` mode (e.g. into or
    out of plan mode) without rebuilding the agent.

    Broadcasts a ``permission_mode_changed`` push so the FE's
    mode badge updates without polling. Returns a short status
    string for the caller (slash command, ``exit_plan_mode``
    tool) to surface.
    """
    from ember_code.core.config.permission_eval import PermissionMode

    if not hasattr(session, "permission_evaluator"):
        return "Error: permission evaluator not initialised yet."
    try:
        new_mode = PermissionMode(mode)
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return f"Error: unknown permission mode {mode!r}. Valid: {valid}"
    prev = session.permission_evaluator.mode
    session.permission_evaluator.mode = new_mode
    if prev == new_mode:
        return f"Permission mode already {new_mode.value}."
    session.broadcast(
        "permission_mode_changed",
        {"mode": new_mode.value, "previous": prev.value},
    )
    return f"Permission mode: {prev.value} → {new_mode.value}."
