"""Knowledge panel widget — interactive search + add over the KB.

Unlike the mostly-read-only MCP / agents / skills / plugins panels,
the knowledge base is interactive: search a query, browse results,
add new content. The widget surfaces both modes:

  * A single input field (default: ``search``). Type a query, press
    Enter, results render below.
  * Toggle to ``add`` mode with ``a``. Same input field now ingests
    URLs / paths / inline text (the backend's ``knowledge_add``
    auto-detects which by shape).

Results carry a name, content preview, and score. Enter on a result
row toggles an expanded view showing the full content.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Literal

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

logger = logging.getLogger(__name__)


__all__ = [
    "KnowledgePanelWidget",
    "KnowledgeSearchHit",
    "KnowledgeStatusInfo",
]


# ── View models ────────────────────────────────────────────────────


class KnowledgeStatusInfo(BaseModel):
    """Panel-side view of the knowledge base status header."""

    enabled: bool = False
    collection_name: str = ""
    document_count: int = 0
    embedder: str = ""


class KnowledgeSearchHit(BaseModel):
    """Panel-side view of one search result."""

    name: str = ""
    content: str = ""
    score: float | None = None
    metadata: dict[str, str] = {}


Mode = Literal["search", "add"]


# ── Widget ─────────────────────────────────────────────────────────


class KnowledgePanelWidget(Widget):
    """Bottom-docked panel — search + add for the knowledge base."""

    can_focus = True

    DEFAULT_CSS = """
    KnowledgePanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        max-height: 28;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    KnowledgePanelWidget .kb-title {
        text-style: bold;
        color: $accent;
    }

    KnowledgePanelWidget .kb-status {
        color: $text-muted;
        margin-bottom: 1;
    }

    KnowledgePanelWidget #kb-input {
        margin: 0 0 1 0;
    }

    KnowledgePanelWidget .kb-results {
        height: auto;
        max-height: 16;
        overflow-y: auto;
    }

    KnowledgePanelWidget .kb-entry {
        padding: 0 1;
        height: auto;
    }

    KnowledgePanelWidget .kb-entry.-selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    KnowledgePanelWidget .kb-empty {
        color: $text-muted;
        padding: 1 0;
    }

    KnowledgePanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class SearchRequested(Message):
        def __init__(self, query: str):
            self.query = query
            super().__init__()

    class AddRequested(Message):
        """User submitted the add input — backend will route by shape
        (URL / path / text)."""

        def __init__(self, source: str):
            self.source = source
            super().__init__()

    class PanelClosed(Message):
        pass

    # ── Reactive state ──────────────────────────────────────────────

    mode: reactive[Mode] = reactive("search")
    selected_index = reactive(0)

    def __init__(
        self,
        status: KnowledgeStatusInfo,
        results: list[KnowledgeSearchHit] | None = None,
    ):
        super().__init__()
        self._status = status
        self._results: list[KnowledgeSearchHit] = results or []
        self._expanded_indices: set[int] = set()
        # When set, ``_status_text`` returns this label (e.g.
        # "Searching for 'foo'…") instead of the static collection
        # metadata. Toggled from the App's RPC handlers around the
        # ``knowledge_search`` / ``knowledge_add`` awaits so the panel
        # doesn't look frozen during the in-flight call.
        self._busy_label: str | None = None

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), classes="kb-title")
        yield Static(self._status_text(), classes="kb-status")
        yield Input(placeholder=self._input_placeholder(), id="kb-input")
        with Vertical(classes="kb-results"):
            yield from self._render_results()
        yield Static(self._hint_text(), classes="hint")

    # ── Render helpers ──────────────────────────────────────────────

    def _title_text(self) -> str:
        return f"[bold $accent]Knowledge · {self.mode}[/bold $accent]"

    def _status_text(self) -> str:
        if self._busy_label:
            return f"[bold $accent]{self._busy_label}[/bold $accent]"
        if not self._status.enabled:
            return "[dim red]Disabled[/dim red] — enable in `knowledge.enabled` config"
        return (
            f"[dim]collection:[/dim] {self._status.collection_name}  "
            f"[dim]·[/dim] {self._status.document_count} docs  "
            f"[dim]·[/dim] {self._status.embedder or '—'}"
        )

    def _input_placeholder(self) -> str:
        if self.mode == "search":
            return "Search the knowledge base — type a query and press Enter"
        return "Add a URL, file path, or inline text — press Enter to ingest"

    def _hint_text(self) -> str:
        if self.mode == "search":
            return (
                "[dim]Enter search · ↑/↓ navigate results · "
                "Enter on result expand · a switch to add · Esc close[/dim]"
            )
        return "[dim]Enter to add · s switch to search · Esc close[/dim]"

    def _render_results(self) -> list[Static]:
        if not self._results:
            empty_text = (
                "Results appear here after a search."
                if self.mode == "search"
                else "Switch to search mode (s) to browse the base."
            )
            return [Static(empty_text, classes="kb-empty")]
        rendered = []
        for i, hit in enumerate(self._results):
            classes = ["kb-entry"]
            if i == self.selected_index:
                classes.append("-selected")
            content = (
                self._render_hit_expanded(hit)
                if i in self._expanded_indices
                else self._render_hit(hit)
            )
            rendered.append(Static(content, id=f"kb-{i}", classes=" ".join(classes)))
        return rendered

    @staticmethod
    def _render_hit(hit: KnowledgeSearchHit) -> str:
        name = hit.name or "[dim italic](untitled)[/dim italic]"
        score = f"  [dim]score {hit.score:.3f}[/dim]" if hit.score is not None else ""
        # 160-char preview on collapsed rows (parallel to skills panel
        # description budget). Strip + collapse whitespace so multi-
        # line entries don't blow up the row height.
        preview = " ".join(hit.content.split())
        if len(preview) > 160:
            preview = preview[:160] + "..."
        return f"  [bold]{name}[/bold]{score}\n      [dim]{preview}[/dim]"

    @staticmethod
    def _render_hit_expanded(hit: KnowledgeSearchHit) -> str:
        lines = [KnowledgePanelWidget._render_hit(hit)]
        if hit.metadata:
            md = ", ".join(f"{k}={v}" for k, v in hit.metadata.items())
            lines.append(f"      [dim]Metadata:[/dim] {md}")
        if hit.content:
            # Full content, indented under the row. Internal scroll
            # on ``.kb-results`` keeps long hits from blowing past
            # ``max-height: 16``.
            indented = "\n".join(f"      {line}" for line in hit.content.rstrip().split("\n"))
            lines.append(f"      [dim]Content:[/dim]\n{indented}")
        return "\n".join(lines)

    # ── Refresh / rebuild ─────────────────────────────────────────

    def set_status(self, status: KnowledgeStatusInfo) -> None:
        self._status = status
        with contextlib.suppress(Exception):
            self.query_one(".kb-status", Static).update(self._status_text())

    def set_busy(self, label: str | None) -> None:
        """Flip the status line to a busy indicator.

        ``label`` non-empty → status shows the label (e.g.
        ``"Searching for 'foo'…"``). ``None`` restores the static
        collection metadata. Must be paired in a try/finally around
        the RPC so a failure doesn't leave the panel stuck spinning.
        """
        self._busy_label = label or None
        with contextlib.suppress(Exception):
            self.query_one(".kb-status", Static).update(self._status_text())

    def set_results(self, results: list[KnowledgeSearchHit]) -> None:
        self._results = results
        self._expanded_indices.clear()
        self.selected_index = 0
        self._rebuild_results()

    def _rebuild_results(self) -> None:
        try:
            container = self.query_one(".kb-results", Vertical)
        except Exception:
            return

        existing: dict[str, Static] = {
            child.id: child  # type: ignore[misc]
            for child in container.children
            if child.id and child.id.startswith("kb-")
        }
        empty_widgets = [
            child for child in container.children if "kb-empty" in (child.classes or set())
        ]

        if not self._results:
            for entry in existing.values():
                entry.remove()
            empty_text = (
                "No results."
                if self.mode == "search"
                else "Switch to search mode (s) to browse the base."
            )
            if not empty_widgets:
                container.mount(Static(empty_text, classes="kb-empty"))
            else:
                empty_widgets[0].update(empty_text)
        else:
            for empty in empty_widgets:
                empty.remove()
            for i, hit in enumerate(self._results):
                widget_id = f"kb-{i}"
                content = (
                    self._render_hit_expanded(hit)
                    if i in self._expanded_indices
                    else self._render_hit(hit)
                )
                if widget_id in existing:
                    existing[widget_id].update(content)
                    if i == self.selected_index:
                        existing[widget_id].add_class("-selected")
                    else:
                        existing[widget_id].remove_class("-selected")
                else:
                    classes = ["kb-entry"]
                    if i == self.selected_index:
                        classes.append("-selected")
                    container.mount(Static(content, id=widget_id, classes=" ".join(classes)))
            for widget_id, child in existing.items():
                try:
                    idx = int(widget_id.split("-")[1])
                    if idx >= len(self._results):
                        child.remove()
                except (ValueError, IndexError):
                    pass

    # ── Watchers ──────────────────────────────────────────────────

    def watch_mode(self, _old: Mode, _new: Mode) -> None:
        with contextlib.suppress(Exception):
            self.query_one(".kb-title", Static).update(self._title_text())
        with contextlib.suppress(Exception):
            inp = self.query_one("#kb-input", Input)
            inp.placeholder = self._input_placeholder()
            inp.value = ""
        with contextlib.suppress(Exception):
            self.query_one(".hint", Static).update(self._hint_text())

    def watch_selected_index(self, old: int, new: int) -> None:
        new_widget: Static | None = None
        for i, marker in ((old, False), (new, True)):
            try:
                widget = self.query_one(f"#kb-{i}", Static)
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """User pressed Enter inside the text input — fire the
        right outbound message based on current mode."""
        value = event.value.strip()
        if not value:
            return
        if self.mode == "search":
            self.post_message(self.SearchRequested(query=value))
        else:
            self.post_message(self.AddRequested(source=value))

    def on_key(self, event) -> None:
        # The Input widget owns most key events when focused. We only
        # handle the panel-level shortcuts that should work regardless
        # of focus.
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.PanelClosed())
            self.remove()
            return

        # When the input is focused, only mode-toggle + result-nav
        # shortcuts that don't conflict with typing fire here. The
        # toggles use ``a`` and ``s`` which would otherwise type into
        # the input — we keep them bound only when the result list
        # is the focused thing (input has its own ``on_key`` and
        # consumes printable keys first; this branch runs when
        # focus is on the panel itself).
        try:
            input_widget = self.query_one("#kb-input", Input)
            if input_widget.has_focus:
                return
        except Exception:
            pass

        if event.key == "a" and self.mode != "add":
            event.stop()
            event.prevent_default()
            self.mode = "add"
        elif event.key == "s" and self.mode != "search":
            event.stop()
            event.prevent_default()
            self.mode = "search"
        elif event.key == "up" and self._results:
            event.stop()
            event.prevent_default()
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down" and self._results:
            event.stop()
            event.prevent_default()
            self.selected_index = min(len(self._results) - 1, self.selected_index + 1)
        elif event.key == "enter" and self._results:
            event.stop()
            event.prevent_default()
            self._toggle_expand_selected()

    def _toggle_expand_selected(self) -> None:
        if not (0 <= self.selected_index < len(self._results)):
            return
        if self.selected_index in self._expanded_indices:
            self._expanded_indices.discard(self.selected_index)
        else:
            self._expanded_indices.add(self.selected_index)
        try:
            widget = self.query_one(f"#kb-{self.selected_index}", Static)
            hit = self._results[self.selected_index]
            content = (
                self._render_hit_expanded(hit)
                if self.selected_index in self._expanded_indices
                else self._render_hit(hit)
            )
            widget.update(content)
        except Exception:
            pass
