"""Panel-details RPCs — agents / hooks / skills / slash commands / output styles.

Extracted from :mod:`ember_code.backend.server`. Six free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates.

The RPCs here all share a shape: read-only snapshot of a
per-session state root, formatted for a specific panel or
completion UI. Kept together because they change together
when a new panel needs a new source of truth.

* :func:`get_agent_details` — agent pool × ephemeral badge
  for the agents panel.
* :func:`get_hooks_details` / :func:`reload_hooks_rpc` —
  session `hooks_map` snapshot + reload trigger for the
  hooks panel.
* :func:`get_skill_details` — skill pool snapshot with full
  bodies for inline expansion.
* :func:`get_slash_commands` — merged builtin + markdown +
  user-invocable-skill list for SDK / IDE completion UIs.
* :func:`get_output_styles` — discovered styles + active
  style for the picker chip.

Rule 2 clean — all inline imports hoisted to module top,
including the module-level `_BUILTIN_DESCRIPTIONS` dict that
was previously living as a class attribute.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.core.pool import AgentInfo
from ember_code.core.skills.parser import SkillInfo
from ember_code.core.utils.markdown_commands import discover_markdown_commands
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


class OutputStyleInfo(BaseModel):
    """One output-style entry in :attr:`OutputStylesResult.styles`."""

    name: str
    description: str


class OutputStylesResult(BaseModel):
    """Wire shape for :func:`get_output_styles` — discovered
    styles + the currently-applied one for the picker chip."""

    active: str
    styles: list[OutputStyleInfo]


# One-liner descriptions for the built-in commands. Used only by
# :func:`get_slash_commands` to give SDK consumers a hint for
# completion UIs — the source of truth for the actual help text
# remains ``CommandHandler._HELP_TOPICS``.
_BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "help": "Show help and available commands",
    "quit": "Exit the session",
    "exit": "Exit the session",
    "clear": "Clear the current conversation",
    "compact": "Compact the conversation context",
    "plan": "Toggle plan mode (read-only sandbox + plan-then-execute workflow)",
    "accept": "Toggle acceptEdits mode (auto-approve file edits)",
    "bypass": "Toggle bypassPermissions mode (auto-approve every tool — no HITL prompts)",
    "output-style": "List or switch the active output style (tone / verbosity)",
    "sessions": "List past sessions",
    "rename": "Rename the current session",
    "fork": "Fork the current session under a new id",
    "model": "Switch the active model",
    "config": "Show current settings",
    "memory": "Inspect or optimise learned memories",
    "knowledge": "Search or add to the knowledge base",
    "codeindex": "Manage the semantic code index",
    "agents": "List and manage agents",
    "skills": "List installed skills",
    "hooks": "List installed hooks",
    "plugins": "Open the plugins panel",
    "plugin": "Install / update / remove a plugin",
    "mcp": "Open the MCP servers panel",
    "login": "Sign in to Ember Cloud",
    "logout": "Sign out of Ember Cloud",
    "whoami": "Show the signed-in user",
    "ctx": "Show context window usage",
    "schedule": "Schedule one-shot or recurring tasks",
    "loop": "Run a prompt in a loop",
    "evals": "Run evaluation suites",
    "bug": "Open a bug report",
    "sync-knowledge": "Sync the knowledge base with git",
}


def get_agent_details(backend: "BackendServer") -> list[AgentInfo]:
    """Snapshot of every loaded agent for the panel UI.

    Combines :meth:`AgentPool.list_agents` with the ephemeral
    directory check so the panel can render the "ephemeral"
    badge + show the promote/discard actions without making a
    second RPC call. Includes the full ``system_prompt`` since
    the panel expands it inline on Enter.
    """
    pool = backend._session.pool
    ephemeral_dir = getattr(pool, "_ephemeral_dir", None)
    results: list[AgentInfo] = []
    for defn in pool.list_agents():
        is_ephemeral = bool(
            ephemeral_dir and defn.source_path and ephemeral_dir in defn.source_path.parents
        )
        results.append(
            AgentInfo(
                name=defn.name,
                description=defn.description,
                tools=list(defn.tools),
                model=defn.model or "",
                color=defn.color or "",
                can_orchestrate=defn.can_orchestrate,
                mcp_servers=list(defn.mcp_servers),
                tags=list(defn.tags),
                system_prompt=defn.system_prompt,
                source_path=str(defn.source_path) if defn.source_path else "",
                is_ephemeral=is_ephemeral,
            )
        )
    return results


def get_hooks_details(backend: "BackendServer") -> list[dict]:
    """Snapshot of every active hook for the hooks panel.

    Walks ``session.hooks_map`` (which is ``{event: [hook, ...]}``
    after the four-root merge + plugin prepend) and flattens
    into one dict per (event, hook) pair. The panel groups
    client-side by ``event`` for display.

    Plain dicts vs. typed wire model because the panel-side
    :class:`HookInfo` lives in the widget — keeping the BE
    side dict-flat avoids a cross-side schema import and the
    fields here are display-only (no behavior depends on the
    type).
    """
    out: list[dict] = []
    for event, hooks in backend._session.hooks_map.items():
        for hook in hooks:
            out.append(
                {
                    "event": str(event),
                    "type": getattr(hook, "type", ""),
                    "command": getattr(hook, "command", "") or "",
                    "url": getattr(hook, "url", "") or "",
                    "matcher": getattr(hook, "matcher", "") or "",
                    "timeout_ms": int(getattr(hook, "timeout", 0) or 0),
                    "background": bool(getattr(hook, "background", False)),
                    "headers": dict(getattr(hook, "headers", {}) or {}),
                }
            )
    return out


def reload_hooks_rpc(backend: "BackendServer") -> msg.Info:
    """Reload hooks from disk. Returns count for the panel toast.

    Distinct name from ``Session.reload_hooks`` so the RPC
    dispatch lambda can reference a stable method here without
    colliding with the session-level helper (which returns an
    int, not the FE-facing ``msg.Info``).
    """
    count = backend._session.reload_hooks()
    return msg.Info(text=f"Reloaded hooks — {count} active hook(s) across all events.")


def get_skill_details(backend: "BackendServer") -> list[SkillInfo]:
    """Snapshot of every loaded skill for the panel UI.

    Sends the full ``body`` (which the panel head-clips for the
    expanded view) so toggling expansion doesn't need an extra
    RPC round trip per row.
    """
    return [
        SkillInfo(
            name=skill.name,
            description=skill.description,
            version=skill.version,
            category=skill.category,
            argument_hint=skill.argument_hint,
            context=skill.context,
            agent=skill.agent or "",
            user_invocable=skill.user_invocable,
            body=skill.body,
            source_dir=str(skill.source_dir) if skill.source_dir else "",
        )
        for skill in backend._session.skill_pool.list_skills()
    ]


def get_output_styles(backend: "BackendServer") -> OutputStylesResult:
    """Snapshot of discovered output styles + the active one.

    FE renders a picker / chip from this. ``active`` is the
    currently-applied style name (empty when none configured).
    """
    styles = getattr(backend._session, "output_styles", {}) or {}
    active = getattr(backend._session, "_active_output_style", "") or ""
    return OutputStylesResult(
        active=active,
        styles=[
            OutputStyleInfo(name=s.name, description=s.description)
            for s in sorted(styles.values(), key=lambda s: s.name)
        ],
    )


def get_slash_commands(backend: "BackendServer") -> list[dict]:
    """Snapshot of every available slash command for SDK consumers
    (IDE plugins, completion UIs, the Claude Code compatibility
    surface).

    Three sources, in stable order:

    1. ``builtin`` — shipped commands from
       ``CommandHandler._COMMANDS`` (always available).
    2. ``markdown`` — files discovered under the four
       ``commands/`` roots (user-tier + project-tier ×
       ember + claude namespaces, gated by
       ``cross_tool_support``).
    3. ``skill`` — user-invocable skills from
       ``session.skill_pool``.

    Each entry: ``{name, description, source, argument_hint}``.
    ``name`` is the bare command (no leading slash) — callers
    prepend the slash when displaying. Mirrors Claude Code's
    SDK ``slash_commands`` field so a CC-compatible client can
    consume both backends uniformly.
    """
    # Late-import ``CommandHandler`` to avoid the circular dep
    # ``backend.command_handler`` → ``backend.server``.
    # ``backend.server_panels`` is intended to be a safe pure
    # dependency, so we keep this one lookup deferred.
    from ember_code.backend.command_handler import CommandHandler

    out: list[dict] = []

    # Built-ins.
    for cmd_name in CommandHandler._COMMANDS:
        bare = cmd_name.lstrip("/")
        out.append(
            {
                "name": bare,
                "description": _BUILTIN_DESCRIPTIONS.get(bare, ""),
                "source": "builtin",
                "argument_hint": "",
            }
        )

    # Markdown-authored commands.
    try:
        read_claude = backend._session.settings.rules.cross_tool_support
        md_commands = discover_markdown_commands(
            backend._session.project_dir,
            read_claude=read_claude,
        )
    except Exception as exc:
        logger.debug("get_slash_commands: markdown discovery failed: %s", exc)
        md_commands = {}
    for md in md_commands.values():
        out.append(
            {
                "name": md.name,
                "description": md.description,
                "source": "markdown",
                "argument_hint": md.argument_hint,
            }
        )

    # User-invocable skills.
    try:
        skills = backend._session.skill_pool.list_skills()
    except Exception as exc:
        logger.debug("get_slash_commands: skill enumeration failed: %s", exc)
        skills = []
    for skill in skills:
        if not getattr(skill, "user_invocable", True):
            continue
        out.append(
            {
                "name": skill.name,
                "description": skill.description,
                "source": "skill",
                "argument_hint": getattr(skill, "argument_hint", ""),
            }
        )

    return out
