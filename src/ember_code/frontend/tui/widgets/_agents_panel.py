"""Agents panel widget — browse loaded agents, promote / discard ephemerals.

Mirrors the structure of :class:`MCPPanelWidget` and
:class:`PluginsPanelWidget`: bottom-docked, single list with
expandable detail on Enter. Promote / discard apply only to ephemeral
agents — surfaced inline via ``p`` / ``d`` key bindings rather than
the slash-command form so the panel is the one place to manage them.
"""

from __future__ import annotations

import contextlib
import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

# Re-export the wire-format model so existing import paths
# (``from widgets import AgentInfo``) keep working — same pattern
# used by the plugins panel.
from ember_code.core.pool import AgentInfo

logger = logging.getLogger(__name__)


__all__ = ["AgentInfo", "AgentsPanelWidget"]


class AgentsPanelWidget(Widget):
    """Bottom-docked panel listing every loaded agent."""

    can_focus = True

    DEFAULT_CSS = """
    AgentsPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 24;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    AgentsPanelWidget .agents-title {
        text-style: bold;
        color: $accent;
    }

    AgentsPanelWidget .agents-list {
        height: auto;
        max-height: 18;
        overflow-y: auto;
    }

    AgentsPanelWidget .agents-entry {
        padding: 0 1;
        height: auto;
    }

    AgentsPanelWidget .agents-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    AgentsPanelWidget .agents-empty {
        color: $text-muted;
        padding: 1 0;
    }

    AgentsPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class PromoteRequested(Message):
        def __init__(self, name: str):
            self.name = name
            super().__init__()

    class DiscardRequested(Message):
        def __init__(self, name: str):
            self.name = name
            super().__init__()

    class PanelClosed(Message):
        pass

    selected_index = reactive(0)

    def __init__(self, agents: list[AgentInfo]):
        super().__init__()
        self._agents = agents
        self._expanded_indices: set[int] = set()

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), classes="agents-title")
        with Vertical(classes="agents-list"):
            yield from self._render_entries()
        yield Static(self._hint_text(), classes="hint")

    def _title_text(self) -> str:
        n = len(self._agents)
        ephemeral = sum(1 for a in self._agents if a.is_ephemeral)
        return f"[bold $accent]Agents[/bold $accent]  [dim]{n} loaded · {ephemeral} ephemeral[/dim]"

    def _hint_text(self) -> str:
        return "[dim]↑/↓ navigate · Enter expand · p promote · d discard · Esc close[/dim]"

    def _render_entries(self) -> list[Static]:
        if not self._agents:
            return [
                Static(
                    "No agents loaded. Add .md files to .ember/agents/ or ~/.ember/agents/.",
                    classes="agents-empty",
                )
            ]
        rendered = []
        for i, agent in enumerate(self._agents):
            classes = ["agents-entry"]
            if i == self.selected_index:
                classes.append("-selected")
            content = (
                self._render_entry_expanded(agent)
                if i in self._expanded_indices
                else self._render_entry(agent)
            )
            rendered.append(Static(content, id=f"agent-{i}", classes=" ".join(classes)))
        return rendered

    # ── Render helpers ──────────────────────────────────────────────

    @staticmethod
    def _render_entry(agent: AgentInfo) -> str:
        tools_count = len(agent.tools)
        tools_str = (
            f"{tools_count} tool{'s' if tools_count != 1 else ''}" if tools_count else "no tools"
        )
        ephemeral_marker = "  [yellow](ephemeral)[/yellow]" if agent.is_ephemeral else ""
        model_str = f" · [dim]{agent.model}[/dim]" if agent.model else ""
        desc = agent.description.strip().split("\n", 1)[0][:60]
        return (
            f"  [bold]{agent.name}[/bold]{model_str}  "
            f"[dim]{tools_str}[/dim]{ephemeral_marker}\n"
            f"      [dim]{desc}[/dim]"
        )

    @staticmethod
    def _render_entry_expanded(agent: AgentInfo) -> str:
        """Selected + expanded — full detail block. Used when the user
        hits Enter on the row."""
        lines = [AgentsPanelWidget._render_entry(agent)]
        if agent.tools:
            lines.append(f"      [dim]Tools:[/dim] {', '.join(agent.tools)}")
        if agent.mcp_servers:
            lines.append(f"      [dim]MCP servers:[/dim] {', '.join(agent.mcp_servers)}")
        if agent.tags:
            lines.append(f"      [dim]Tags:[/dim] {', '.join(agent.tags)}")
        if agent.source_path:
            lines.append(f"      [dim]Source:[/dim] {agent.source_path}")
        if agent.system_prompt:
            # First 240 chars of system prompt — enough to recognize the
            # agent's role without flooding the panel. Full prompt is
            # always one Read away from the source file.
            preview = agent.system_prompt.strip().replace("\n", " ")
            if len(preview) > 240:
                preview = preview[:240] + "…"
            lines.append(f"      [dim]Prompt:[/dim] {preview}")
        return "\n".join(lines)

    # ── Refresh / rebuild ─────────────────────────────────────────

    def refresh_agents(self, agents: list[AgentInfo]) -> None:
        self._agents = agents
        self.selected_index = min(self.selected_index, max(0, len(self._agents) - 1))
        self._rebuild()

    def _rebuild(self) -> None:
        """Update list entries + title in place. Same update-vs-mount
        pattern as the MCP/plugins panels — Textual's ``remove()`` is
        async, so naive remove-then-mount hits ``DuplicateIds``.
        """
        try:
            container = self.query_one(".agents-list", Vertical)
            title = self.query_one(".agents-title", Static)
        except Exception:
            return

        existing: dict[str, Static] = {
            child.id: child  # type: ignore[misc]
            for child in container.children
            if child.id and child.id.startswith("agent-")
        }
        empty_widgets = [
            child for child in container.children if "agents-empty" in (child.classes or set())
        ]

        if not self._agents:
            for entry in existing.values():
                entry.remove()
            if not empty_widgets:
                container.mount(
                    Static(
                        "No agents loaded.",
                        classes="agents-empty",
                    )
                )
        else:
            for empty in empty_widgets:
                empty.remove()
            for i, agent in enumerate(self._agents):
                widget_id = f"agent-{i}"
                content = (
                    self._render_entry_expanded(agent)
                    if i in self._expanded_indices
                    else self._render_entry(agent)
                )
                if widget_id in existing:
                    existing[widget_id].update(content)
                    if i == self.selected_index:
                        existing[widget_id].add_class("-selected")
                    else:
                        existing[widget_id].remove_class("-selected")
                else:
                    classes = ["agents-entry"]
                    if i == self.selected_index:
                        classes.append("-selected")
                    container.mount(
                        Static(
                            content,
                            id=widget_id,
                            classes=" ".join(classes),
                        )
                    )
            for widget_id, child in existing.items():
                try:
                    idx = int(widget_id.split("-")[1])
                    if idx >= len(self._agents):
                        child.remove()
                except (ValueError, IndexError):
                    pass

        title.update(self._title_text())

    # ── Watchers ──────────────────────────────────────────────────

    def watch_selected_index(self, old: int, new: int) -> None:
        new_widget: Static | None = None
        for i, marker in ((old, False), (new, True)):
            try:
                widget = self.query_one(f"#agent-{i}", Static)
                if marker:
                    widget.add_class("-selected")
                    new_widget = widget
                else:
                    widget.remove_class("-selected")
            except Exception:
                pass
        # Auto-scroll the newly-selected row into view so arrow nav
        # past the visible window doesn't hide the selection.
        if new_widget is not None:
            with contextlib.suppress(Exception):
                new_widget.scroll_visible(animate=False)

    # ── Input ─────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()

        if event.key == "escape":
            self.post_message(self.PanelClosed())
            self.remove()
            return

        if not self._agents:
            return

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._agents) - 1, self.selected_index + 1)
        elif event.key == "enter":
            self._toggle_expand_selected()
        elif event.key == "p":
            self._promote_selected()
        elif event.key == "d":
            self._discard_selected()

    def _toggle_expand_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._agents)):
            return
        if self.selected_index in self._expanded_indices:
            self._expanded_indices.discard(self.selected_index)
        else:
            self._expanded_indices.add(self.selected_index)
        try:
            widget = self.query_one(f"#agent-{self.selected_index}", Static)
            agent = self._agents[self.selected_index]
            content = (
                self._render_entry_expanded(agent)
                if self.selected_index in self._expanded_indices
                else self._render_entry(agent)
            )
            widget.update(content)
        except Exception:
            pass

    def _promote_selected(self) -> None:
        """Promote only fires for ephemeral agents — regular agents are
        already permanent. Silently ignored on non-ephemeral selection
        so a stray ``p`` press doesn't surface a confusing error."""
        if not (0 <= self.selected_index < len(self._agents)):
            return
        agent = self._agents[self.selected_index]
        if not agent.is_ephemeral:
            return
        self.post_message(self.PromoteRequested(name=agent.name))

    def _discard_selected(self) -> None:
        """Same constraint as promote — discard only applies to
        ephemerals."""
        if not (0 <= self.selected_index < len(self._agents)):
            return
        agent = self._agents[self.selected_index]
        if not agent.is_ephemeral:
            return
        self.post_message(self.DiscardRequested(name=agent.name))

    def on_click(self, event) -> None:
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._agents)):
            try:
                widget = self.query_one(f"#agent-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception:
                pass
