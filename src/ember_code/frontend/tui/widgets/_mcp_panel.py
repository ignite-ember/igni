"""MCP panel widget — browse and toggle MCP server connections."""

import contextlib
import logging

from pydantic import BaseModel, Field
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


class MCPServerInfo(BaseModel):
    """Snapshot of an MCP server's state for the panel UI."""

    name: str
    connected: bool
    transport: str = "stdio"
    tool_names: list[str] = Field(default_factory=list)
    tool_descriptions: dict[str, str] = Field(default_factory=dict)
    error: str = ""
    policy_blocked: bool = False


class MCPPanelWidget(Widget):
    """Bottom-docked panel for browsing and toggling MCP servers.

    Navigate with Up/Down, toggle with Space/Enter, close with Escape.
    """

    can_focus = True

    DEFAULT_CSS = """
    MCPPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 20;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    MCPPanelWidget .mcp-title {
        text-style: bold;
        color: $accent;
    }

    MCPPanelWidget .mcp-list {
        height: auto;
        max-height: 14;
        overflow-y: auto;
    }

    MCPPanelWidget .mcp-entry {
        padding: 0 1;
        height: auto;
    }

    MCPPanelWidget .mcp-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    MCPPanelWidget .mcp-empty {
        color: $text-muted;
        padding: 1 0;
    }

    MCPPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    class ServerToggleRequested(Message):
        """Posted when the user toggles a server on/off."""

        def __init__(self, name: str, enable: bool):
            self.name = name
            self.enable = enable
            super().__init__()

    class PanelClosed(Message):
        pass

    selected_index = reactive(0)

    def __init__(self, servers: list[MCPServerInfo]):
        super().__init__()
        self._servers = servers

    def compose(self) -> ComposeResult:
        connected = sum(1 for s in self._servers if s.connected)
        total = len(self._servers)
        yield Static(
            f"[bold $accent]MCP Servers[/bold $accent]  "
            f"[dim]{connected} connected / {total} total[/dim]",
            classes="mcp-title",
        )
        with Vertical(classes="mcp-list"):
            if not self._servers:
                yield Static(
                    "No MCP servers configured. Add servers to .mcp.json",
                    classes="mcp-empty",
                )
            else:
                for i, server in enumerate(self._servers):
                    classes = ["mcp-entry"]
                    if i == self.selected_index:
                        classes.append("-selected")
                    yield Static(
                        self._render_entry(server),
                        id=f"mcp-{i}",
                        classes=" ".join(classes),
                    )
        yield Static(
            "[dim]↑/↓ navigate · Space toggle · Enter expand tools · Esc close[/dim]",
            classes="hint",
        )

    @staticmethod
    def _render_entry(server: MCPServerInfo) -> str:
        if server.policy_blocked:
            return f"  [dim]🔒 {server.name}[/dim]  [dim]{server.transport}[/dim]  [red]blocked by policy[/red]"
        if server.connected:
            tool_count = len(server.tool_names)
            return (
                f"  [green]●[/green] {server.name}  "
                f"[dim]{server.transport}[/dim]  "
                f"[dim]{tool_count} tool{'s' if tool_count != 1 else ''}[/dim]"
            )
        if server.error:
            short_err = server.error[:50] + ("..." if len(server.error) > 50 else "")
            return f"  [red]○[/red] {server.name}  [dim]{server.transport}[/dim]  [red]{short_err}[/red]"
        return (
            f"  [red]○[/red] {server.name}  [dim]{server.transport}[/dim]  [dim]disconnected[/dim]"
        )

    def _render_entry_expanded(self, server: MCPServerInfo) -> str:
        base = self._render_entry(server)
        if not server.connected or not server.tool_names:
            return base
        lines = [base]
        for tname in server.tool_names:
            desc = server.tool_descriptions.get(tname, "")
            if desc:
                lines.append(f"      [dim]{tname}[/dim] — {desc}")
            else:
                lines.append(f"      [dim]{tname}[/dim]")
        return "\n".join(lines)

    def refresh_servers(self, servers: list[MCPServerInfo]) -> None:
        """Update the panel with fresh server data."""
        self._servers = servers
        self.selected_index = min(self.selected_index, max(0, len(self._servers) - 1))
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild the list entries in place."""
        try:
            container = self.query_one(".mcp-list", Vertical)
        except Exception:
            return
        # Update existing entries in place instead of remove+mount (avoids DuplicateIds)
        if not self._servers:
            # Remove all entries and show empty message
            for child in list(container.children):
                child.remove()
            container.mount(
                Static(
                    "No MCP servers configured. Add servers to .mcp.json",
                    classes="mcp-empty",
                )
            )
            return
        # Update or create entries
        existing = {child.id: child for child in container.children if child.id}
        for i, server in enumerate(self._servers):
            widget_id = f"mcp-{i}"
            content = self._render_entry(server)
            if widget_id in existing:
                # Update in place
                existing[widget_id].update(content)
                if i == self.selected_index:
                    existing[widget_id].add_class("-selected")
                else:
                    existing[widget_id].remove_class("-selected")
            else:
                classes = ["mcp-entry"]
                if i == self.selected_index:
                    classes.append("-selected")
                container.mount(Static(content, id=widget_id, classes=" ".join(classes)))
        # Remove excess entries
        for widget_id, child in existing.items():
            if widget_id and widget_id.startswith("mcp-"):
                try:
                    idx = int(widget_id.split("-")[1])
                    if idx >= len(self._servers):
                        child.remove()
                except (ValueError, IndexError):
                    pass
        # Update title
        connected = sum(1 for s in self._servers if s.connected)
        total = len(self._servers)
        try:
            title = self.query_one(".mcp-title", Static)
            title.update(
                f"[bold $accent]MCP Servers[/bold $accent]  "
                f"[dim]{connected} connected / {total} total[/dim]"
            )
        except Exception:
            pass

    def watch_selected_index(self, old: int, new: int) -> None:
        try:
            old_widget = self.query_one(f"#mcp-{old}", Static)
            old_widget.remove_class("-selected")
            old_widget.update(self._render_entry(self._servers[old]))
        except Exception:
            pass
        try:
            new_widget = self.query_one(f"#mcp-{new}", Static)
            new_widget.add_class("-selected")
            # Keep the highlighted row visible — arrow nav past the
            # viewport would otherwise hide the selection. No
            # animation: rapid down-presses would stack jitters.
            with contextlib.suppress(Exception):
                new_widget.scroll_visible(animate=False)
        except Exception:
            pass

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()

        if not self._servers:
            if event.key in ("escape", "enter"):
                self.post_message(self.PanelClosed())
                self.remove()
            return

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._servers) - 1, self.selected_index + 1)
        elif event.key == "space":
            self._toggle_selected()
        elif event.key == "enter":
            self._expand_selected()
        elif event.key == "escape":
            self.post_message(self.PanelClosed())
            self.remove()

    def _toggle_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._servers)):
            return
        server = self._servers[self.selected_index]
        if server.policy_blocked:
            return
        self.post_message(self.ServerToggleRequested(name=server.name, enable=not server.connected))

    def _expand_selected(self) -> None:
        """Toggle tool list expansion on the selected entry."""
        if not (0 <= self.selected_index < len(self._servers)):
            return
        server = self._servers[self.selected_index]
        if not hasattr(self, "_expanded_indices"):
            self._expanded_indices: set[int] = set()
        try:
            widget = self.query_one(f"#mcp-{self.selected_index}", Static)
            if self.selected_index in self._expanded_indices:
                widget.update(self._render_entry(server))
                self._expanded_indices.discard(self.selected_index)
            else:
                widget.update(self._render_entry_expanded(server))
                self._expanded_indices.add(self.selected_index)
        except Exception:
            pass

    def on_click(self, event) -> None:
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._servers)):
            try:
                widget = self.query_one(f"#mcp-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception:
                pass
