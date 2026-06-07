"""Skills panel widget — browse loaded skills with run / expand.

Mirrors :class:`AgentsPanelWidget` — bottom-docked, single list,
expandable detail on Enter. Skills are grouped by category in the
list header so users can scan by domain (development / review /
planning / operations). Hitting ``r`` runs the selected skill with
empty arguments — useful when the skill prompts for args itself or
has sensible defaults.
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

from ember_code.core.skills.parser import SkillInfo

logger = logging.getLogger(__name__)


__all__ = ["SkillInfo", "SkillsPanelWidget"]


class SkillsPanelWidget(Widget):
    """Bottom-docked panel listing every loaded skill."""

    can_focus = True

    DEFAULT_CSS = """
    SkillsPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 24;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    SkillsPanelWidget .skills-title {
        text-style: bold;
        color: $accent;
    }

    SkillsPanelWidget .skills-list {
        height: auto;
        max-height: 18;
        overflow-y: auto;
    }

    SkillsPanelWidget .skills-entry {
        padding: 0 1;
        height: auto;
    }

    SkillsPanelWidget .skills-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    SkillsPanelWidget .skills-empty {
        color: $text-muted;
        padding: 1 0;
    }

    SkillsPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class RunRequested(Message):
        """User wants to invoke the selected skill (empty arguments).

        The app dispatches this through the main controller so the
        skill renders into the conversation just like a typed
        ``/skill-name`` slash command would.
        """

        def __init__(self, name: str):
            self.name = name
            super().__init__()

    class PanelClosed(Message):
        pass

    selected_index = reactive(0)

    def __init__(self, skills: list[SkillInfo]):
        super().__init__()
        self._skills = skills
        self._expanded_indices: set[int] = set()

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), classes="skills-title")
        with Vertical(classes="skills-list"):
            yield from self._render_entries()
        yield Static(self._hint_text(), classes="hint")

    def _title_text(self) -> str:
        n = len(self._skills)
        # Distinct category count surfaced so the user can tell at a
        # glance how broad the skill set is.
        categories = {s.category for s in self._skills if s.category}
        return (
            f"[bold $accent]Skills[/bold $accent]  "
            f"[dim]{n} loaded · {len(categories)} categor"
            f"{'y' if len(categories) == 1 else 'ies'}[/dim]"
        )

    def _hint_text(self) -> str:
        return "[dim]↑/↓ navigate · Enter expand · r run · Esc close[/dim]"

    def _render_entries(self) -> list[Static]:
        if not self._skills:
            return [
                Static(
                    "No skills loaded. Add SKILL.md files to .ember/skills/ or ~/.ember/skills/.",
                    classes="skills-empty",
                )
            ]
        rendered = []
        for i, skill in enumerate(self._skills):
            classes = ["skills-entry"]
            if i == self.selected_index:
                classes.append("-selected")
            content = (
                self._render_entry_expanded(skill)
                if i in self._expanded_indices
                else self._render_entry(skill)
            )
            rendered.append(Static(content, id=f"skill-{i}", classes=" ".join(classes)))
        return rendered

    # ── Render helpers ──────────────────────────────────────────────

    _DESC_MAX = 160  # collapsed-row description budget (chars)

    @staticmethod
    def _render_entry(skill: SkillInfo) -> str:
        hint = f" [dim]{skill.argument_hint}[/dim]" if skill.argument_hint else ""
        cat = f" [dim]{skill.category}[/dim]" if skill.category else ""
        not_invocable = "  [dim](agent-only)[/dim]" if not skill.user_invocable else ""
        first_line = skill.description.strip().split("\n", 1)[0]
        if len(first_line) > SkillsPanelWidget._DESC_MAX:
            desc = first_line[: SkillsPanelWidget._DESC_MAX].rstrip() + "..."
        else:
            desc = first_line
        return f"  [bold]/{skill.name}[/bold]{hint}{cat}{not_invocable}\n      [dim]{desc}[/dim]"

    @staticmethod
    def _render_entry_expanded(skill: SkillInfo) -> str:
        lines = [SkillsPanelWidget._render_entry(skill)]
        if skill.version:
            lines.append(f"      [dim]Version:[/dim] {skill.version}")
        if skill.context and skill.context != "inline":
            lines.append(f"      [dim]Context:[/dim] {skill.context}")
        if skill.agent:
            lines.append(f"      [dim]Agent:[/dim] {skill.agent}")
        if skill.source_dir:
            lines.append(f"      [dim]Source:[/dim] {skill.source_dir}")
        if skill.body:
            # Show the full body verbatim. Skills are short by
            # construction (they're prompt templates, not docs), and
            # the panel's ``max-height: 24`` plus internal scroll on
            # ``.skills-list`` keeps long bodies from blowing past the
            # screen. Indent each line so the body visually nests under
            # its row.
            indented = "\n".join(f"      {line}" for line in skill.body.rstrip().split("\n"))
            lines.append(f"      [dim]Body:[/dim]\n{indented}")
        return "\n".join(lines)

    # ── Refresh / rebuild ─────────────────────────────────────────

    def refresh_skills(self, skills: list[SkillInfo]) -> None:
        self._skills = skills
        self.selected_index = min(self.selected_index, max(0, len(self._skills) - 1))
        self._rebuild()

    def _rebuild(self) -> None:
        """Update list entries + title in place. Same in-place pattern
        as the agents/plugins/MCP panels."""
        try:
            container = self.query_one(".skills-list", Vertical)
            title = self.query_one(".skills-title", Static)
        except Exception:
            return

        existing: dict[str, Static] = {
            child.id: child  # type: ignore[misc]
            for child in container.children
            if child.id and child.id.startswith("skill-")
        }
        empty_widgets = [
            child for child in container.children if "skills-empty" in (child.classes or set())
        ]

        if not self._skills:
            for entry in existing.values():
                entry.remove()
            if not empty_widgets:
                container.mount(Static("No skills loaded.", classes="skills-empty"))
        else:
            for empty in empty_widgets:
                empty.remove()
            for i, skill in enumerate(self._skills):
                widget_id = f"skill-{i}"
                content = (
                    self._render_entry_expanded(skill)
                    if i in self._expanded_indices
                    else self._render_entry(skill)
                )
                if widget_id in existing:
                    existing[widget_id].update(content)
                    if i == self.selected_index:
                        existing[widget_id].add_class("-selected")
                    else:
                        existing[widget_id].remove_class("-selected")
                else:
                    classes = ["skills-entry"]
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
                    if idx >= len(self._skills):
                        child.remove()
                except (ValueError, IndexError):
                    pass

        title.update(self._title_text())

    # ── Watchers ──────────────────────────────────────────────────

    def watch_selected_index(self, old: int, new: int) -> None:
        new_widget: Static | None = None
        for i, marker in ((old, False), (new, True)):
            try:
                widget = self.query_one(f"#skill-{i}", Static)
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

        if not self._skills:
            return

        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self._skills) - 1, self.selected_index + 1)
        elif event.key == "enter":
            self._toggle_expand_selected()
        elif event.key == "r":
            self._run_selected()

    def _toggle_expand_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._skills)):
            return
        if self.selected_index in self._expanded_indices:
            self._expanded_indices.discard(self.selected_index)
        else:
            self._expanded_indices.add(self.selected_index)
        try:
            widget = self.query_one(f"#skill-{self.selected_index}", Static)
            skill = self._skills[self.selected_index]
            content = (
                self._render_entry_expanded(skill)
                if self.selected_index in self._expanded_indices
                else self._render_entry(skill)
            )
            widget.update(content)
        except Exception:
            pass

    def _run_selected(self) -> None:
        """Run only fires for user-invocable skills. Skills marked
        ``user_invocable: false`` are agent-only — silently ignored so
        a stray ``r`` press on an agent-only entry doesn't post a
        confusing run."""
        if not (0 <= self.selected_index < len(self._skills)):
            return
        skill = self._skills[self.selected_index]
        if not skill.user_invocable:
            return
        self.post_message(self.RunRequested(name=skill.name))

    def on_click(self, event) -> None:
        target = event.widget if hasattr(event, "widget") else None
        if target is None:
            return
        for i in range(len(self._skills)):
            try:
                widget = self.query_one(f"#skill-{i}", Static)
                if target is widget or target.is_descendant_of(widget):
                    self.selected_index = i
                    return
            except Exception:
                pass
