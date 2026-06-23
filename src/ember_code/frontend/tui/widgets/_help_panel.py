"""Help panel widget — interactive expandable help sections."""

from __future__ import annotations

import contextlib

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class HelpSection:
    """A help topic with title, summary, and expandable details."""

    def __init__(self, title: str, summary: str, details: str):
        self.title = title
        self.summary = summary
        self.details = details


_CMD_WIDTH = 32  # fixed width for command column


def _row(cmd: str, desc: str) -> str:
    """Format a help row with fixed-width command column."""
    # Escape [ for Rich markup (Rich treats [text] as tags)
    display = cmd.replace("[", "\\[")
    pad = _CMD_WIDTH - len(cmd)
    if pad < 1:
        pad = 1
    return f"      {display}{' ' * pad}— {desc}"


HELP_SECTIONS = [
    HelpSection(
        "Getting Started",
        "Basic usage and key concepts",
        "Just describe what you need in plain language.\n"
        "igni picks the right agents and tools automatically.\n\n"
        + "\n".join(
            [
                _row("@filename", "reference project files with autocomplete"),
                _row("\\ + Enter", "new line (Enter sends)"),
                _row("Escape", "cancel running operation"),
                _row("Ctrl+D", "quit"),
            ]
        ),
    ),
    HelpSection(
        "Agents",
        "/agents — list, create, promote, discard",
        "Agents are specialist roles with tools and system prompts.\n\n"
        + "\n".join(
            [
                _row("/agents", "list all loaded agents"),
                _row("/agents ephemeral", "list dynamically created agents"),
                _row("/agents promote <name>", "save ephemeral agent permanently"),
                _row("/agents discard <name>", "delete an ephemeral agent"),
            ]
        )
        + "\n\nCreate agents by adding .md files to .ember/agents/",
    ),
    HelpSection(
        "Skills",
        "/skills — reusable workflow commands",
        "Skills are slash commands that run multi-step workflows.\n\n"
        + "\n".join(
            [
                _row("/commit [message]", "create a git commit"),
                _row("/pr [base-branch]", "create PR with AI summary"),
                _row(
                    "/resolve-issues [base-branch]", "fix issues CodeIndex flagged in your branch"
                ),
                _row("/test-plan [target]", "generate test plan"),
                _row("/migration <desc>", "generate database migration"),
            ]
        )
        + "\n\nCreate skills by adding SKILL.md files to .ember/skills/",
    ),
    HelpSection(
        "Schedule",
        "/schedule — deferred and recurring tasks",
        "Schedule tasks for later or recurring execution.\n\n"
        + "\n".join(
            [
                _row("/schedule", "open task panel"),
                _row("/schedule <desc> at <time>", "one-shot task"),
                _row("/schedule <desc> in <dur>", "relative time"),
                _row("/schedule <desc> every <n>", "recurring task"),
                _row("/schedule show <id>", "task details"),
                _row("/schedule cancel <id>", "cancel a task"),
            ]
        )
        + "\n\nTimes: at 5pm, in 30 minutes, tomorrow at 9am,\n"
        "       every 2 hours, daily, weekly",
    ),
    HelpSection(
        "Knowledge",
        "/knowledge — project knowledge base",
        "Store and search project knowledge with embeddings.\n\n"
        + "\n".join(
            [
                _row("/knowledge", "show status"),
                _row("/knowledge add <url>", "add a URL"),
                _row("/knowledge add <path>", "add a file/directory"),
                _row("/knowledge add <text>", "add inline text"),
                _row("/knowledge search <q>", "search the knowledge base"),
                _row("/sync-knowledge", "sync with git-tracked file"),
            ]
        ),
    ),
    HelpSection(
        "CodeIndex",
        "/codeindex — live status panel for the current commit",
        "Per-commit AI summaries fetched from Ember Cloud and applied to a\n"
        "local Chroma index. The panel polls the indexed-state of the\n"
        "current commit every couple of seconds, so a sync in progress\n"
        "shows live %. Search lives on the slash command — its markdown\n"
        "results belong in chat history, not an ephemeral panel.\n\n"
        + "\n".join(
            [
                _row("/codeindex", "open the live status panel"),
                _row("(in panel) S / C / I", "sync · clean · install action keys"),
                _row("/codeindex search <q>", "semantic search the indexed commit"),
                _row("/codeindex install", "open the GitHub App install page"),
                _row("/codeindex sync", "pull + apply the current commit"),
                _row("/codeindex item <id>", "full details for one item"),
                _row("/codeindex commits", "list locally-indexed commits as markdown"),
                _row("/codeindex status", "show sync state and install state"),
                _row("/codeindex clean", "drop stale, non-branch commits"),
            ]
        )
        + "\n\nAuto-syncs on startup, /clear, and HEAD changes (git pull, branch switch).",
    ),
    HelpSection(
        "Memory",
        "/memory — what Ember has learned about you",
        "Ember learns your preferences automatically from conversations.\n\n"
        + "\n".join(
            [
                _row("/memory", "show your profile and memories"),
                _row("/memory optimize", "consolidate memories"),
            ]
        )
        + "\n\nLearned automatically: name, language preferences,\n"
        "frameworks, coding style, project conventions.\n"
        "Extraction runs in the background after each response.",
    ),
    HelpSection(
        "MCP Servers",
        "/mcp — external tool connections",
        "Connect external tools via the Model Context Protocol.\n\n"
        + "\n".join(
            [
                _row("/mcp", "open the MCP panel"),
                _row("  Space", "toggle connect/disconnect"),
                _row("  Enter", "expand tool list"),
                _row("  Escape", "close panel"),
            ]
        )
        + "\n\nConfigure in .mcp.json:\n"
        '  {"mcpServers": {"name": {"type": "stdio", "command": "..."}}}\n'
        "Transports: stdio and sse",
    ),
    HelpSection(
        "Plugins",
        "/plugins, /plugin — Claude-Code-compatible bundles",
        "Plugins bundle skills, agents, hooks, MCP servers, and tools\n"
        "into one installable unit. Bundles built for Claude Code work\n"
        "in igni unchanged.\n\n"
        + "\n".join(
            [
                _row("/plugins", "open the panel"),
                _row("  Space", "toggle enable / disable"),
                _row("  u / r", "update / remove"),
                _row("  Tab", "switch tabs"),
                _row("  i", "install selected"),
                _row("  R", "refresh catalogs"),
                _row("  Escape", "close panel"),
                _row("/plugins enable <name>", "enable (no panel)"),
                _row("/plugins disable <name>", "disable (no panel)"),
                _row("/plugin install <url>", "install from URL"),
                _row("/plugin install @<m>/<p>", "install via marketplace"),
                _row("/plugin … --ref X", "pin to branch / tag / sha"),
                _row("/plugin update <name>", "fetch + reset to HEAD"),
                _row("/plugin remove <name>", "uninstall"),
                _row("/plugin marketplace add <url>", "register marketplace"),
                _row("/plugin marketplace list", "show marketplaces"),
                _row("/plugin marketplace remove <n>", "unregister"),
                _row("/plugin marketplace refresh", "re-fetch catalogs"),
            ]
        )
        + "\n\nRoots: ~/.ember/plugins, ~/.claude/plugins, and the project\n"
        "equivalents. Toggles + install / update / remove take effect on\n"
        "next session start.",
    ),
    HelpSection(
        "Loop",
        "/loop — re-fire a prompt across turns",
        "Repeat a prompt as the next user turn. Useful for *do X for\n"
        "each of A, B, C* or *keep fixing failures until tests pass*.\n\n"
        + "\n".join(
            [
                _row("/loop", "open the live status panel (X cancel, R resume)"),
                _row("/loop <prompt>", "start (cap: 30)"),
                _row("/loop <N> <prompt>", "start with cap N"),
                _row("/loop stop", "cancel from chat"),
                _row("/loop resume", "continue an interrupted loop after restart"),
            ]
        )
        + "\n\nStops on: /loop stop · any non-/loop input · iteration cap\n"
        "(default 30, hard limit 200) · agent calling loop_stop().",
    ),
    HelpSection(
        "Hooks",
        "/hooks — event-triggered command + HTTP callbacks",
        "Run a command or POST to a URL when an event fires (a tool\n"
        "is about to execute, a session starts, the user submits a\n"
        "prompt, etc.). Configured in the four ``settings.json``\n"
        "files; the panel surfaces what's currently loaded.\n\n"
        + "\n".join(
            [
                _row("/hooks", "open the hooks panel"),
                _row("(in panel) Enter", "expand a row (full command, headers)"),
                _row("(in panel) R", "reload from disk after editing settings"),
                _row("/hooks list", "markdown list to chat (scripting)"),
                _row("/hooks reload", "reload hooks from settings (chat)"),
            ]
        )
        + "\n\nDefined in: ~/.ember/settings.json, .ember/settings.json\n"
        "(and their .local.json overrides). Plugin-bundled hooks\n"
        "merge in automatically.",
    ),
    HelpSection(
        "Sessions",
        "/sessions, /clear, /rename, /compact",
        "\n".join(
            [
                _row("/sessions", "browse and resume past sessions"),
                _row("/clear", "reset conversation (new session)"),
                _row("/rename <name>", "rename current session"),
                _row("/compact", "summarize and trim context"),
                _row("--continue", "resume last session on start"),
            ]
        ),
    ),
    HelpSection(
        "Configuration",
        "/config, /model",
        "\n".join(
            [
                _row("/config", "show current settings"),
                _row("/model [name]", "switch or pick model"),
            ]
        )
        + "\n\nConfig files:\n"
        + "\n".join(
            [
                _row("~/.ember/config.yaml", "global settings"),
                _row(".ember/config.yaml", "project settings"),
                _row("ember.md", "project context for agents"),
            ]
        ),
    ),
    HelpSection(
        "Other",
        "/login, /logout, /whoami, /bug, /evals",
        "\n".join(
            [
                _row("/login", "authenticate with Ember Cloud"),
                _row("/logout", "clear credentials"),
                _row("/whoami", "show auth status"),
                _row("/bug", "report an issue on GitHub"),
                _row("/evals [agent]", "run agent evaluations"),
            ]
        ),
    ),
]


class HelpPanelWidget(Widget):
    """Bottom-docked interactive help panel with expandable sections."""

    can_focus = True

    DEFAULT_CSS = """
    HelpPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 22;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    HelpPanelWidget .help-title {
        text-style: bold;
        color: $accent;
    }

    HelpPanelWidget .help-list {
        height: auto;
        max-height: 18;
        overflow-y: auto;
    }

    HelpPanelWidget .help-entry {
        padding: 0 1;
        height: auto;
    }

    HelpPanelWidget .help-entry.-selected {
        background: $accent;
        color: $text;
    }

    HelpPanelWidget .hint {
        color: $text-muted;
        height: 1;
    }
    """

    selected_index: reactive[int] = reactive(0)

    class PanelClosed(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sections = HELP_SECTIONS
        self._expanded: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold $accent]Help[/bold $accent]  [dim]{len(self._sections)} topics[/dim]",
            classes="help-title",
        )
        with Vertical(classes="help-list"):
            for i, section in enumerate(self._sections):
                classes = ["help-entry"]
                if i == 0:
                    classes.append("-selected")
                yield Static(
                    self._render_entry(i, section),
                    id=f"help-{i}",
                    classes=" ".join(classes),
                )
        yield Static(
            "[dim]↑/↓ navigate · Enter expand/collapse · Esc close[/dim]",
            classes="hint",
        )

    def _render_entry(self, index: int, section: HelpSection) -> str:
        arrow = "▼" if index in self._expanded else "▶"
        line = f"  {arrow} [bold]{section.title}[/bold] — [dim]{section.summary}[/dim]"
        if index in self._expanded:
            line += f"\n{section.details}"
        return line

    def watch_selected_index(self, old: int, new: int) -> None:
        try:
            old_widget = self.query_one(f"#help-{old}", Static)
            old_widget.remove_class("-selected")
            old_widget.update(self._render_entry(old, self._sections[old]))
        except Exception:
            pass
        try:
            new_widget = self.query_one(f"#help-{new}", Static)
            new_widget.add_class("-selected")
            new_widget.update(self._render_entry(new, self._sections[new]))
            # Keep the highlighted row in view — arrow nav past
            # the visible window would otherwise hide the selection.
            with contextlib.suppress(Exception):
                new_widget.scroll_visible(animate=False)
        except Exception:
            pass

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._sections) - 1, self.selected_index + 1)
        elif event.key == "enter":
            self._toggle_expand()
        elif event.key == "escape":
            self.post_message(self.PanelClosed())
            self.remove()

    def _toggle_expand(self) -> None:
        idx = self.selected_index
        if idx in self._expanded:
            self._expanded.discard(idx)
        else:
            self._expanded.add(idx)
        try:
            widget = self.query_one(f"#help-{idx}", Static)
            widget.update(self._render_entry(idx, self._sections[idx]))
        except Exception:
            pass
