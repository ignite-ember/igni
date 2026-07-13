"""``/memory``, ``/knowledge``, ``/sync_knowledge`` slash commands.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands that share the "read/write the session's persistent
learnings" surface:

* ``/memory`` — show Learning Machine data (user profile /
  memory / entity memory / session context). ``/memory
  optimize`` triggers a compaction pass over stored memories.
* ``/knowledge`` — open the panel or add a URL / path / text
  to the knowledge base.
* ``/sync_knowledge`` — bidirectional cloud sync (when
  ``knowledge.share`` is enabled).

Each function takes ``CommandHandler`` as its first argument
and reads/writes via ``handler._session.memory_mgr`` /
``knowledge_mgr`` / ``main_team.learning_machine``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


async def cmd_memory(handler: "CommandHandler", args: str) -> "CommandResult":
    """Show Learning Machine data or trigger memory compaction."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    subcommand = args.strip().lower()

    if subcommand == "optimize":
        result = await handler._session.memory_mgr.optimize()
        if "error" in result:
            return CommandResult.error(f"Memory optimization failed: {result['error']}")
        return CommandResult.info(result["message"])

    # Show Learning Machine data — use agent's property which triggers
    # lazy init.
    learning = getattr(handler._session.main_team, "learning_machine", None)
    if learning is None:
        learning = getattr(handler._session, "_learning", None)
    if learning is None:
        return CommandResult.info(
            "Learning is not enabled. Set learning.enabled=true in config."
        )

    sections: list[str] = []
    try:
        # Recall with session_id=None to get cross-session data (user
        # profile, user memory, entity memory).
        data = await learning.arecall(
            user_id=handler._session.user_id,
        )
        for store_name, store_data in data.items():
            if not store_data:
                continue
            title = store_name.replace("_", " ").title()
            lines = f"## {title}\n"

            if store_name == "user_profile":
                for attr in ("name", "preferred_name", "role", "expertise", "preferences"):
                    val = getattr(store_data, attr, None)
                    if val:
                        lines += f"- **{attr.replace('_', ' ').title()}**: {val}\n"

            elif store_name == "user_memory":
                memories = getattr(store_data, "memories", []) or []
                for m in memories:
                    content = m.get("content", "") if isinstance(m, dict) else str(m)
                    if content:
                        lines += f"- {content}\n"

            elif store_name == "session_context":
                summary = getattr(store_data, "summary", None)
                if summary:
                    lines += f"{summary}\n"

            elif store_name == "entity_memory":
                entities = getattr(store_data, "entities", []) or []
                for e in entities:
                    if isinstance(e, dict):
                        lines += f"- **{e.get('name', '?')}**: {e.get('description', '')}\n"
                    else:
                        lines += f"- {e}\n"

            else:
                lines += f"{store_data}\n"

            if lines.strip() != f"## {title}":
                sections.append(lines)
    except Exception:
        pass

    if not sections:
        return CommandResult.info(
            "No learnings stored yet. The agent learns from your conversations automatically."
        )

    return CommandResult.markdown("\n\n".join(sections))


async def cmd_knowledge(handler: "CommandHandler", args: str) -> "CommandResult":
    """Handle ``/knowledge`` commands: add url|path|text, search, status."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    mgr = handler._session.knowledge_mgr
    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else ""
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    if subcommand == "add" and sub_args:
        if sub_args.startswith(("http://", "https://")):
            result = await mgr.add_url(sub_args)
        elif "/" in sub_args or sub_args.startswith("."):
            result = await mgr.add_path(sub_args)
        else:
            result = await mgr.add(text=sub_args)
        if not result.success:
            return CommandResult.error(result.error)
        return CommandResult.info(result.message)

    if subcommand == "search":
        # Open the panel — the input field there IS the search UI.
        # Pre-populating from the slash command would defeat the
        # purpose: the panel is where users type queries, iterate,
        # and browse results interactively. ``sub_args`` (if any)
        # is ignored on purpose; users continue typing in the panel.
        return CommandResult.knowledge()

    # No subcommand: open the TUI panel. (Status + commands hint were
    # previously printed as markdown — the panel surfaces the same
    # status header and lets the user search / add interactively.)
    # The error path is preserved so users see a clear reason when
    # the base failed to initialize.
    status = await mgr.status()
    if not status.enabled:
        if handler._session.settings.knowledge.enabled:
            if handler._session._knowledge_error:
                return CommandResult.error(
                    f"Knowledge failed to load: {handler._session._knowledge_error}"
                )
            return CommandResult.error("Knowledge base failed to initialize.")
        return CommandResult.info(
            "Knowledge base is disabled. Set knowledge.enabled=true in config."
        )
    return CommandResult.knowledge()


async def cmd_sync_knowledge(handler: "CommandHandler") -> "CommandResult":
    """Handle ``/sync_knowledge`` — bidirectional cloud sync."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    if not handler._session.knowledge_mgr.share_enabled():
        return CommandResult.info(
            "Knowledge sharing is not enabled. Set knowledge.share=true in config."
        )
    results = await handler._session.knowledge_mgr.sync_bidirectional()
    lines = [f"[{r.direction}] {r.summary}" for r in results]
    return CommandResult.info("\n".join(lines))
