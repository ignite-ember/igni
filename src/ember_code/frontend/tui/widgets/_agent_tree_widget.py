"""Agent tree widget — visualises the orchestrator's team plan.

Extracted from ``_messages.py`` (iter 39) per Pattern 8. Displays
the top-level team, its coordination mode, and each spawned
agent with the optional reasoning string underneath.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Tree


class AgentTreeWidget(Widget):
    """Displays the orchestrator's team plan as a tree."""

    DEFAULT_CSS = """
    AgentTreeWidget {
        height: auto;
        max-height: 12;
        margin: 0 2 1 2;
        padding: 0;

    }

    AgentTreeWidget .tree-header {
        color: $accent;
        text-style: bold;
    }

    AgentTreeWidget Tree {
        height: auto;
        max-height: 10;
    }
    """

    def __init__(
        self,
        team_name: str,
        team_mode: str,
        agent_names: list[str],
        reasoning: str = "",
    ):
        super().__init__()
        self._team_name = team_name
        self._team_mode = team_mode
        self._agent_names = agent_names
        self._reasoning = reasoning

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold $accent]Team:[/bold $accent] {self._team_name} [dim]({self._team_mode})[/dim]",
            classes="tree-header",
        )
        tree: Tree[str] = Tree(self._team_name)
        tree.root.expand()

        tree.root.add(f"[dim]mode:[/dim] {self._team_mode}")

        agents_node = tree.root.add("[bold]agents[/bold]", expand=True)
        for name in self._agent_names:
            agents_node.add_leaf(f"[green]{name}[/green]")

        if self._reasoning:
            short = self._reasoning[:120]
            if len(self._reasoning) > 120:
                short += "..."
            tree.root.add(f"[dim]reason:[/dim] {short}")

        yield tree
