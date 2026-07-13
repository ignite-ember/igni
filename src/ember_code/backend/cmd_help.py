"""``/help`` slash command + topic corpus.

Extracted from :mod:`ember_code.backend.command_handler` — the
``_HELP_TOPICS`` dict was ~180 LoC of markdown-formatted help
strings hanging on the god-file. Moving them here lets the god-
file drop 180+ lines and keeps the topic corpus visible in one
place for future edits.

``/help`` with no argument opens the interactive TUI panel; with
a topic name it renders the matching markdown block to chat.
Unknown topics get a "did you mean" list.

The ``SHORTCUT_HELP`` string is imported from
``command_handler`` (the canonical copy — the TUI input handler
has its own parallel copy for its shortcut widget).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


def _help_topics() -> dict[str, str]:
    """Return the topic → markdown dict.

    Built lazily on first ``/help <topic>`` call so the import
    of ``command_handler.SHORTCUT_HELP`` doesn't fire at module
    load time — that would create a cycle
    (`cmd_help` ← `command_handler` ← `cmd_help`).
    """
    from ember_code.backend.command_handler import SHORTCUT_HELP

    return {
        "schedule": (
            "## Schedule\n\n"
            "Schedule tasks for later or recurring execution.\n\n"
            "**Commands:**\n"
            "- `/schedule` — list pending tasks\n"
            "- `/schedule all` — include completed and cancelled\n"
            "- `/schedule <description> at <time>` — one-shot task\n"
            "- `/schedule <description> in <duration>` — relative time\n"
            "- `/schedule <description> every <interval>` — recurring\n"
            "- `/schedule show <id>` — show task details\n"
            "- `/schedule cancel <id>` — cancel a task\n\n"
            "*The `add` keyword is optional — any phrasing with a time clause is treated as a new task.*\n\n"
            "**Time formats:**\n"
            "- One-shot: `at 5pm`, `at 3:30`, `tomorrow`, `tomorrow at 9am`, `2026-12-25 14:00`\n"
            "- Relative: `in 30 minutes`, `in 2 hours`, `in 1 day`\n"
            "- Recurring: `every 2 hours`, `every 30 minutes`, `daily`, `daily at 9am`, `hourly`, `weekly`\n\n"
            "**Examples:**\n"
            "```\n"
            "/schedule review code at 5pm\n"
            "/schedule run tests in 30 minutes\n"
            "/schedule check deps daily\n"
            "/schedule run linter every 2 hours\n"
            "```"
        ),
        "loop": (
            "## Loop\n\n"
            "Repeat a prompt over and over in the current session. The "
            "next iteration starts immediately after the previous one "
            'ends — no gap. Useful for *"do X for each of A, B, C"*, '
            '*"keep fixing failures until the tests pass"*, *"go '
            'through these one at a time"*.\n\n'
            "**Commands:**\n"
            "- `/loop <prompt>` — start (default cap: 30 iterations)\n"
            "- `/loop <N> <prompt>` — start with explicit cap N\n"
            "- `/loop stop` — cancel the active loop\n"
            "- `/loop` — show status (active loop, iterations done/remaining)\n\n"
            "**Stops on:**\n"
            "- `/loop stop` from the user\n"
            "- Any non-`/loop` user input (treated as an interrupt)\n"
            "- The iteration cap (default 30, hard limit 200)\n"
            "- The agent calling `loop_stop()` mid-conversation\n\n"
            "**Plain-language control:** the agent has `loop_start` / "
            '`loop_stop` tools, so you can say *"loop this 5 times"* '
            'or *"stop the loop"* in normal chat and the agent will '
            "translate to the right call.\n\n"
            "**Differs from `/schedule`:** schedule fires on a cron clock "
            "and runs headlessly. `/loop` fires immediately and streams "
            "every iteration live in your TUI."
        ),
        "plugins": (
            "## Plugins\n\n"
            "Claude-Code-compatible plugin support. A plugin is a "
            "directory bundling skills, agents, hooks, MCP servers, "
            "and/or custom tools that activate together. Plugins "
            "built for Claude Code work in Ember unchanged.\n\n"
            "**Discovery roots (highest priority last):**\n"
            "- `~/.claude/plugins/` (Claude user-global)\n"
            "- `~/.ember/plugins/` (ember user-global — where `/plugin install` lands)\n"
            "- `<project>/.claude/plugins/`\n"
            "- `<project>/.ember/plugins/`\n\n"
            "**Daily commands:**\n"
            "- `/plugins` — open the TUI panel (browse, toggle, install)\n"
            "- `/plugins enable <name>` / `/plugins disable <name>` — toggle without opening the panel\n"
            "- `/plugin install <git-url>` — install from a git URL into `~/.ember/plugins/`\n"
            "- `/plugin install @<marketplace>/<plugin>` — install via marketplace\n"
            "- `/plugin install <url> --ref <branch|tag|sha>` — pin at install time\n"
            "- `/plugin update <name>` — fetch + reset to origin's HEAD\n"
            "- `/plugin remove <name>` — uninstall (deletes the plugin dir)\n\n"
            "**Marketplaces (Claude-Code-compatible catalogs):**\n"
            "- `/plugin marketplace add <git-url>` — register a marketplace\n"
            "- `/plugin marketplace list` — show registered marketplaces\n"
            "- `/plugin marketplace remove <name>` — unregister (installed plugins remain)\n"
            "- `/plugin marketplace refresh [<name>]` — re-fetch one or all\n\n"
            "Enable / disable / install / update / remove all take effect "
            "on next session start. See [Plugins](PLUGINS.md) for the "
            "full guide."
        ),
        "agents": (
            "## Agents\n\n"
            "Agents are specialist roles with tools and system prompts.\n\n"
            "**Commands:**\n"
            "- `/agents` — list all loaded agents with tools\n"
            "- `/agents ephemeral` — list dynamically created agents\n"
            "- `/agents promote <name>` — save ephemeral agent permanently\n"
            "- `/agents discard <name>` — delete an ephemeral agent\n\n"
            "**Create agents:** add `.md` files to `.ember/agents/`\n"
            "**Customize:** edit any agent in `.ember/agents/` to change its behavior"
        ),
        "knowledge": (
            "## Knowledge Base\n\n"
            "Store and search project knowledge with embeddings.\n\n"
            "**Commands:**\n"
            "- `/knowledge` — show status (collection, doc count, embedder)\n"
            "- `/knowledge add <url>` — add a URL\n"
            "- `/knowledge add <path>` — add a file or directory\n"
            "- `/knowledge add <text>` — add inline text\n"
            "- `/knowledge search <query>` — search the knowledge base\n"
            "- `/sync-knowledge` — sync between git file and vector DB"
        ),
        "codeindex": (
            "## CodeIndex\n\n"
            "Semantic code intelligence over your repo. The Ember GitHub App "
            "indexes every commit on the server and ships per-commit changesets "
            "to your machine, where they're applied to a local Chroma index. "
            "Search runs entirely locally — your code summaries never leave "
            "your laptop after the initial fetch.\n\n"
            "**Setup (one-time):**\n"
            "- `/codeindex install` — open the GitHub App install page,\n"
            "  pre-pointed at your current repo. Click **Install** and you're done.\n\n"
            "**Daily commands:**\n"
            "- `/codeindex sync` — pull and apply the changeset for the current commit\n"
            "- `/codeindex resync` — wipe local state and re-pull a full snapshot\n"
            "- `/codeindex search <query>` — semantic search the indexed commit\n"
            "- `/codeindex item <id>` — full details for one item\n"
            "- `/codeindex commits` — list locally-indexed commits\n"
            "- `/codeindex clean` — drop stale, non-branch commits\n"
            "- `/codeindex status` — show local HEAD, last-synced sha, install state\n\n"
            "**Auto-sync:**\n"
            "Sync fires on app startup, on `/clear`, and whenever your local "
            "HEAD moves (after `git pull`, branch switch, etc.) — no manual "
            "trigger needed in the steady state."
        ),
        "memory": (
            "## Memory & Learning\n\n"
            "igni learns your preferences automatically from conversations.\n\n"
            "**Commands:**\n"
            "- `/memory` — show what Ember has learned about you\n"
            "- `/memory optimize` — consolidate memories\n\n"
            "**What gets learned:**\n"
            "- Your name and how you prefer to be addressed\n"
            "- Tool and framework preferences (pytest, ruff, Pydantic, etc.)\n"
            "- Project structure conventions (src/ layout, etc.)\n"
            "- Coding style preferences (type hints, etc.)\n\n"
            "Learning happens in the background after each response."
        ),
        "mcp": (
            "## MCP Servers\n\n"
            "Connect external tools via the Model Context Protocol.\n\n"
            "**Commands:**\n"
            "- `/mcp` — open the MCP panel (browse, connect, disconnect)\n\n"
            "**Configuration:** add servers to `.mcp.json`:\n"
            "```json\n"
            '{"mcpServers": {"name": {"type": "stdio", "command": "npx", "args": [...]}}}\n'
            "```\n"
            "**Transports:** `stdio` and `sse` supported\n"
            "**Panel controls:** Space toggle, Enter expand tools, Escape close"
        ),
        "hooks": (
            "## Hooks\n\n"
            "Run a command or POST to a URL when an event fires "
            "(before a tool runs, when a session starts, when the "
            "user submits a prompt, etc.). Hooks gate or audit "
            "behavior — return `should_continue=false` from a "
            "command hook to block the triggering action.\n\n"
            "**Commands:**\n"
            "- `/hooks` — open the hooks panel (group by event, expand for full command, `R` to reload)\n"
            "- `/hooks list` — markdown list to chat (scripting-friendly)\n"
            "- `/hooks reload` — re-read settings files (chat output)\n\n"
            "**Events** (see `core/hooks/events.py` for the full enum):\n"
            "- `PreToolUse`, `PostToolUse`, `PostToolUseFailure`\n"
            "- `UserPromptSubmit`, `SessionStart`, `SessionEnd`\n"
            "- `Stop`, `SubagentStart`, `SubagentStop`, `Notification`\n\n"
            "**Defined in** (four-root cascade, last wins):\n"
            "- `~/.ember/settings.json` (global)\n"
            "- `~/.ember/settings.local.json` (global, gitignored)\n"
            "- `.ember/settings.json` (project, committed)\n"
            "- `.ember/settings.local.json` (project, gitignored)\n\n"
            "Plugins also contribute hooks via `<plugin>/hooks/hooks.json`; "
            "plugin hooks are prepended per event so project hooks get "
            "the final word.\n\n"
            "**Hook config keys:**\n"
            "- `type`: `command` or `http`\n"
            "- `command` / `url`: what to run\n"
            "- `headers`: HTTP headers (for `http` type)\n"
            "- `matcher`: regex over tool name (empty = all)\n"
            "- `timeout`: ms before the hook is killed (default 10000)\n"
            "- `background`: fire-and-forget (don't block the agent)"
        ),
        "shortcuts": SHORTCUT_HELP,
    }


async def cmd_help(handler: "CommandHandler", args: str) -> "CommandResult":
    """``/help`` command — open panel or render a topic to chat."""
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    CommandResult = _handler.CommandResult
    topic = args.strip().lower()
    topics = _help_topics()

    # Topic-specific help.
    if topic and topic in topics:
        return CommandResult.markdown(topics[topic])

    # List available topics if unknown.
    if topic:
        available = ", ".join(sorted(topics.keys()))
        return CommandResult.error(f"Unknown help topic: {topic}. Available: {available}")

    # No topic: show interactive panel.
    return CommandResult(kind=CommandResultKind.INFO, content="", action=CommandAction.HELP)
