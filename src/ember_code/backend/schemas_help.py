"""Typed data + view models for the ``/help`` slash command.

Extracted from :mod:`ember_code.backend.cmd_help` — the old
``_help_topics()`` returned a raw ``dict[str, str]`` of 10
markdown blocks and the "unknown topic" error was ad-hoc string
glue at the call site. Both live here now as Pydantic-owned
classes so the topic corpus and the error view are single-concern
class-owned pieces of behaviour (Rule 1 + Rule 6).

The :class:`HelpTopicCatalog` is the canonical owner of the topic
corpus; :class:`HelpTopic` renders one topic to a
:class:`CommandResult`; :class:`TopicNotFoundResult` builds the
"Unknown help topic" error result.

The ``shortcuts`` topic pulls its markdown from
:class:`KeyboardShortcutsHelp` at module import time — no cycle
because :mod:`keyboard_shortcuts_help` imports nothing from
:mod:`command_handler` or :mod:`cmd_help`.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator

from ember_code.backend.command_result import CommandResult
from ember_code.backend.keyboard_shortcuts_help import KeyboardShortcutsHelp


class HelpTopic(BaseModel):
    """One entry in the ``/help`` topic corpus.

    Owns both the topic name and the markdown block that
    :meth:`to_command_result` renders — Rule 1: data lives with
    the code that renders it.
    """

    name: str
    markdown: str

    def to_command_result(self) -> CommandResult:
        return CommandResult.markdown(self.markdown)


class TopicNotFoundResult(BaseModel):
    """View model for the "Unknown help topic: X. Available: ..." error.

    Replaces the ad-hoc ``", ".join(sorted(topics.keys()))`` string
    glue from the old ``cmd_help`` body. The sorted-available
    contract is enforced by :meth:`HelpTopicCatalog.names`.
    """

    attempted: str
    available: list[str]

    def to_command_result(self) -> CommandResult:
        joined = ", ".join(self.available)
        return CommandResult.error(f"Unknown help topic: {self.attempted}. Available: {joined}")


class HelpTopicCatalog(BaseModel):
    """Class-owned corpus of ``/help`` topics.

    Instantiate via :meth:`default` for the canonical 10-entry
    catalog; query with :meth:`get` / :meth:`contains` / :meth:`names`;
    render via :meth:`render` (dispatches to a topic or to
    :class:`TopicNotFoundResult`).
    """

    topics: list[HelpTopic]

    @model_validator(mode="after")
    def _reject_duplicate_names(self) -> HelpTopicCatalog:
        """Refuse to build a catalog with duplicate topic names — the
        first-match lookup in :meth:`get` would silently mask the
        second entry, so an invariant here catches the bug at
        construction rather than at lookup time.
        """
        seen: set[str] = set()
        for topic in self.topics:
            if topic.name in seen:
                raise ValueError(f"Duplicate help topic: {topic.name!r}")
            seen.add(topic.name)
        return self

    @classmethod
    def default(cls) -> HelpTopicCatalog:
        """Build the canonical 10-entry topic corpus.

        Markdown blocks are moved verbatim from the old
        ``_help_topics()`` dict; the ``shortcuts`` block pulls
        from :meth:`KeyboardShortcutsHelp.markdown` at call time.
        """
        return cls(
            topics=[
                HelpTopic(
                    name="schedule",
                    markdown=(
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
                ),
                HelpTopic(
                    name="loop",
                    markdown=(
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
                ),
                HelpTopic(
                    name="plugins",
                    markdown=(
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
                ),
                HelpTopic(
                    name="agents",
                    markdown=(
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
                ),
                HelpTopic(
                    name="knowledge",
                    markdown=(
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
                ),
                HelpTopic(
                    name="codeindex",
                    markdown=(
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
                ),
                HelpTopic(
                    name="memory",
                    markdown=(
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
                ),
                HelpTopic(
                    name="mcp",
                    markdown=(
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
                ),
                HelpTopic(
                    name="hooks",
                    markdown=(
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
                ),
                HelpTopic(
                    name="shortcuts",
                    markdown=KeyboardShortcutsHelp.markdown(),
                ),
            ]
        )

    def get(self, name: str) -> HelpTopic | None:
        """Return the topic named ``name`` or ``None`` if missing."""
        for topic in self.topics:
            if topic.name == name:
                return topic
        return None

    def contains(self, name: str) -> bool:
        """True when ``name`` is one of the catalog's topic names."""
        return self.get(name) is not None

    def names(self) -> list[str]:
        """Sorted list of topic names — used by the not-found error
        so the "Available: ..." list is deterministic."""
        return sorted(topic.name for topic in self.topics)

    def render(self, name: str) -> CommandResult:
        """Render topic ``name`` — dispatches to the topic on hit,
        or to :class:`TopicNotFoundResult` on miss."""
        topic = self.get(name)
        if topic is not None:
            return topic.to_command_result()
        return self.render_unknown(name)

    def render_unknown(self, name: str) -> CommandResult:
        """Build the "Unknown help topic" error result for ``name``."""
        return TopicNotFoundResult(attempted=name, available=self.names()).to_command_result()


__all__ = ["HelpTopic", "HelpTopicCatalog", "TopicNotFoundResult"]
