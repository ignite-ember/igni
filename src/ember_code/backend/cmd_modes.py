"""Permission-mode slash commands: ``/plan``, ``/accept``, ``/bypass``.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands that share the same shape (toggle the session's live
`PermissionEvaluator.mode` without rebuilding the agent) but each
flip into a different mode:

* ``/plan`` — enter/exit ``PermissionMode.PLAN``. File edits +
  mutating shell blocked; agent should call ``exit_plan_mode``
  when ready.
* ``/accept`` — enter/exit ``PermissionMode.ACCEPT_EDITS``. File
  edits auto-approve; shell still asks. For trusted-loop
  workflows.
* ``/bypass`` — enter/exit ``PermissionMode.BYPASS_PERMISSIONS``.
  ANY tool auto-approves. For fully-autonomous loops.

Bypass-resistant scoped denies (from row 9 — e.g.
``deny: save_file(.env)``) still hold in every mode. Only the
per-tool ``ask`` step is skipped; deny always wins.

All three commands accept the same arg vocabulary:

* (empty) / ``toggle`` — flip target ↔ default.
* ``on`` / ``enable`` / ``start`` — enter target.
* ``off`` / ``disable`` / ``exit`` / ``stop`` — return to
  default.
* ``status`` / ``show`` — report the current mode without
  changing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.config.permission_eval import PermissionMode

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


async def cmd_plan(handler: "CommandHandler", args: str) -> "CommandResult":
    """Toggle the session in/out of plan mode (CC parity, row 50).

    The mode flip mutates the live ``Session.permission_evaluator``
    so it takes effect on the very next tool call — no agent
    rebuild, no restart.
    """
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandResultKind

    CommandResult = _handler.CommandResult
    normalized = args.strip().lower()
    evaluator = getattr(handler._session, "permission_evaluator", None)
    if evaluator is None:
        return CommandResult.error("Permission evaluator not initialised.")
    current = evaluator.mode

    if normalized in ("on", "enable", "start"):
        target = PermissionMode.PLAN
    elif normalized in ("off", "disable", "exit", "stop"):
        target = PermissionMode.DEFAULT
    elif normalized in ("", "toggle"):
        # Toggle: plan ↔ default. Other modes (acceptEdits,
        # bypassPermissions, dontAsk) all flip TO plan on toggle
        # — entering plan from any non-plan mode is the only
        # sensible read.
        target = (
            PermissionMode.DEFAULT if current is PermissionMode.PLAN else PermissionMode.PLAN
        )
    elif normalized in ("status", "show"):
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Permission mode: **{current.value}**",
        )
    else:
        return CommandResult.error(
            f"Unknown /plan argument: {normalized!r}. Use /plan, /plan on, /plan off, or /plan status."
        )

    status_line = handler._session.set_permission_mode(target.value)
    if target is PermissionMode.PLAN:
        # Arm a one-shot researcher nudge for the NEXT user
        # message, but ONLY on the transition INTO plan mode. If
        # the user types ``/plan`` while already in plan mode
        # (e.g. to re-confirm), don't re-arm — the researcher
        # already ran on this session's first post-``/plan`` turn
        # and re-running it just spends tokens.
        if current is not PermissionMode.PLAN:
            handler._session._plan_research_armed = True
        tail = (
            "\n\nYou are now in **plan mode**. The agent can read, "
            "search, and think but file edits and mutating shell "
            "commands are blocked. The agent should call "
            "`exit_plan_mode(plan)` when ready; use `/plan` again "
            "to exit plan mode and let it execute."
        )
    else:
        # Leaving plan mode also disarms the pending researcher
        # nudge — if the user changed their mind between ``/plan``
        # and a follow-up message, we don't want a stale hint to
        # fire on their next turn.
        if hasattr(handler._session, "_plan_research_armed"):
            handler._session._plan_research_armed = False
        tail = "\n\nPlan mode exited. The agent can now execute as normal."
    return CommandResult(
        kind=CommandResultKind.MARKDOWN,
        content=status_line + tail,
    )


async def cmd_accept(handler: "CommandHandler", args: str) -> "CommandResult":
    """Toggle the session in/out of acceptEdits mode (CC parity, row 51).

    Unlike plan mode, ``acceptEdits`` is LOOSENING the sandbox —
    the agent intentionally does NOT get a tool to flip itself
    in. Only the user can opt in via this slash command.
    Bypass-resistant scoped denies (row 9) still hold — even in
    acceptEdits, a ``deny`` rule like ``save_file(.env)`` still
    blocks.
    """
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandResultKind

    CommandResult = _handler.CommandResult
    normalized = args.strip().lower()
    evaluator = getattr(handler._session, "permission_evaluator", None)
    if evaluator is None:
        return CommandResult.error("Permission evaluator not initialised.")
    current = evaluator.mode

    if normalized in ("on", "enable", "start"):
        target = PermissionMode.ACCEPT_EDITS
    elif normalized in ("off", "disable", "exit", "stop"):
        target = PermissionMode.DEFAULT
    elif normalized in ("", "toggle"):
        target = (
            PermissionMode.DEFAULT
            if current is PermissionMode.ACCEPT_EDITS
            else PermissionMode.ACCEPT_EDITS
        )
    elif normalized in ("status", "show"):
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Permission mode: **{current.value}**",
        )
    else:
        return CommandResult.error(
            f"Unknown /accept argument: {normalized!r}. "
            "Use /accept, /accept on, /accept off, or /accept status."
        )

    status_line = handler._session.set_permission_mode(target.value)
    if target is PermissionMode.ACCEPT_EDITS:
        tail = (
            "\n\nYou are now in **acceptEdits mode**. File-edit "
            "tools (`edit_file`, `save_file`, `create_file`) "
            "auto-approve without per-tool HITL prompts — useful "
            "for autonomous loops on trusted work. Scoped denies "
            "still block specific files (e.g. `.env`); use "
            "`/accept off` to leave the mode."
        )
    else:
        tail = "\n\nacceptEdits exited. Edits now require approval per the default policy."
    return CommandResult(
        kind=CommandResultKind.MARKDOWN,
        content=status_line + tail,
    )


async def cmd_bypass(handler: "CommandHandler", args: str) -> "CommandResult":
    """Toggle the session in/out of bypassPermissions mode.

    In ``bypassPermissions`` mode the permission evaluator skips
    the HITL prompt for ANY tool — shell, edits, MCP — letting
    the agent run autonomously without per-tool approval. Used
    from the footer "Auto-approve" switch for trusted loops.
    Scoped denies (row 9) still hold — only the per-tool ``ask``
    step is skipped.
    """
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandResultKind

    CommandResult = _handler.CommandResult
    normalized = args.strip().lower()
    evaluator = getattr(handler._session, "permission_evaluator", None)
    if evaluator is None:
        return CommandResult.error("Permission evaluator not initialised.")
    current = evaluator.mode

    if normalized in ("on", "enable", "start"):
        target = PermissionMode.BYPASS_PERMISSIONS
    elif normalized in ("off", "disable", "exit", "stop"):
        target = PermissionMode.DEFAULT
    elif normalized in ("", "toggle"):
        target = (
            PermissionMode.DEFAULT
            if current is PermissionMode.BYPASS_PERMISSIONS
            else PermissionMode.BYPASS_PERMISSIONS
        )
    elif normalized in ("status", "show"):
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Permission mode: **{current.value}**",
        )
    else:
        return CommandResult.error(
            f"Unknown /bypass argument: {normalized!r}. "
            "Use /bypass, /bypass on, /bypass off, or /bypass status."
        )

    status_line = handler._session.set_permission_mode(target.value)
    if target is PermissionMode.BYPASS_PERMISSIONS:
        tail = (
            "\n\nYou are now in **bypassPermissions mode**. The "
            "agent will run any tool without asking — useful for "
            "autonomous loops on trusted work. Scoped denies still "
            "block specific files (e.g. `.env`); use `/bypass off` "
            "to leave the mode."
        )
    else:
        tail = (
            "\n\nbypassPermissions exited. Tools now require approval per the default policy."
        )
    return CommandResult(
        kind=CommandResultKind.MARKDOWN,
        content=status_line + tail,
    )
