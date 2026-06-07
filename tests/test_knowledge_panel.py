"""Tests for ``KnowledgePanelWidget``.

The knowledge panel is interactive (search + add) so the test harness
drives the Input widget rather than just key events. Asserts on the
message-bus contract (SearchRequested / AddRequested) and the
render-helper branches that change behavior.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input

from ember_code.frontend.tui.widgets._knowledge_panel import (
    KnowledgePanelWidget,
    KnowledgeSearchHit,
    KnowledgeStatusInfo,
)

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(
        self,
        status: KnowledgeStatusInfo | None = None,
        results: list[KnowledgeSearchHit] | None = None,
    ) -> None:
        super().__init__()
        self._status = status or KnowledgeStatusInfo(
            enabled=True,
            collection_name="shared",
            document_count=42,
            embedder="all-MiniLM-L6-v2",
        )
        self._results = results or []
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield KnowledgePanelWidget(status=self._status, results=self._results)

    def on_knowledge_panel_widget_search_requested(self, m) -> None:
        self.captured.append(("search", m.query))

    def on_knowledge_panel_widget_add_requested(self, m) -> None:
        self.captured.append(("add", m.source))

    def on_knowledge_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


def _hit(name: str, content: str = "", score: float = 0.5) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(name=name, content=content, score=score)


# ── Input submission ───────────────────────────────────────────────


async def test_enter_in_search_mode_posts_search_request() -> None:
    """Default mode is ``search``. Typing into the Input and pressing
    Enter posts ``SearchRequested`` with the query."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        inp = panel.query_one("#kb-input", Input)
        inp.focus()
        await pilot.pause()
        # Submit programmatically — Pilot key presses are unreliable
        # for Input widgets across Textual versions.
        inp.value = "auth"
        await inp.action_submit()
        await pilot.pause()
        assert ("search", "auth") in app.captured


async def test_enter_in_add_mode_posts_add_request() -> None:
    """`a` toggles to add mode. Submit in add mode posts
    ``AddRequested`` instead of ``SearchRequested``."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        # Focus the panel itself so ``a`` doesn't get typed into the input.
        panel.focus()
        await pilot.press("a")
        assert panel.mode == "add"
        inp = panel.query_one("#kb-input", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "https://example.com/docs"
        await inp.action_submit()
        await pilot.pause()
        assert ("add", "https://example.com/docs") in app.captured


async def test_empty_submission_is_noop() -> None:
    """Submitting an empty / whitespace-only query doesn't post any
    event — avoids spawning an empty search that the backend would
    reject or return zero hits for."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        inp = panel.query_one("#kb-input", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "   "
        await inp.action_submit()
        await pilot.pause()
        assert app.captured == []


# ── Mode toggle ─────────────────────────────────────────────────────


async def test_a_and_s_toggle_mode_when_input_unfocused() -> None:
    """Mode shortcuts only fire when the input is NOT focused —
    otherwise they'd be typed as characters into the input field."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        panel.focus()
        await pilot.press("a")
        assert panel.mode == "add"
        await pilot.press("s")
        assert panel.mode == "search"


async def test_mode_toggle_clears_input() -> None:
    """Switching modes clears the input — search queries and add
    sources are different shapes, so dragging text across modes
    would mostly produce wrong submissions."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        inp = panel.query_one("#kb-input", Input)
        inp.value = "stale query"
        panel.focus()
        await pilot.press("a")
        await pilot.pause()
        assert inp.value == ""


# ── Result navigation + expansion ─────────────────────────────────


async def test_arrow_keys_navigate_results() -> None:
    app = _Host(
        results=[
            _hit("a", content="content a"),
            _hit("b", content="content b"),
            _hit("c", content="content c"),
        ]
    )
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        panel.focus()
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        for _ in range(5):
            await pilot.press("down")
        assert panel.selected_index == 2


async def test_enter_on_result_toggles_expansion() -> None:
    """Enter on a result row (input unfocused) toggles the expanded
    content view. Re-entering collapses it back."""
    app = _Host(results=[_hit("a", content="Long content body.")])
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        panel.focus()
        assert 0 not in panel._expanded_indices
        await pilot.press("enter")
        assert 0 in panel._expanded_indices
        await pilot.press("enter")
        assert 0 not in panel._expanded_indices


# ── Render helpers ─────────────────────────────────────────────────


async def test_hit_renders_name_and_score() -> None:
    app = _Host(results=[_hit("auth.py", score=0.873)])
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        rendered = panel._render_hit(panel._results[0])
        assert "auth.py" in rendered
        assert "0.873" in rendered


async def test_hit_handles_untitled() -> None:
    """Anonymous hits (no ``name``) still render — they get an
    italic ``(untitled)`` placeholder so the row isn't blank."""
    app = _Host(results=[_hit("", content="content")])
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        rendered = panel._render_hit(panel._results[0])
        assert "untitled" in rendered.lower()


async def test_hit_preview_clipped_at_160() -> None:
    """Collapsed preview clips at 160 chars + ``...`` — parallel to
    the skills panel description budget."""
    long_content = "Z" * 300
    app = _Host(results=[_hit("doc", content=long_content)])
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        rendered = panel._render_hit(panel._results[0])
        assert "Z" * 160 in rendered
        assert "Z" * 161 not in rendered
        assert "..." in rendered


async def test_expanded_shows_full_content() -> None:
    """Expanded view shows the full content (no clipping). Panel's
    internal scroll handles long entries."""
    long_content = "Y" * 800
    app = _Host(results=[_hit("doc", content=long_content)])
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        expanded = panel._render_hit_expanded(panel._results[0])
        # Full 800 Y's present in the expanded form.
        assert "Y" * 800 in expanded


# ── Status header ─────────────────────────────────────────────────


async def test_status_renders_doc_count_and_collection() -> None:
    status = KnowledgeStatusInfo(
        enabled=True,
        collection_name="my-project",
        document_count=137,
        embedder="all-MiniLM-L6-v2",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        text = panel._status_text()
        assert "my-project" in text
        assert "137" in text


async def test_status_when_disabled_says_so() -> None:
    """Disabled state surfaces a clear "Disabled" badge so the user
    knows the panel is read-only / non-functional until config is
    flipped."""
    app = _Host(status=KnowledgeStatusInfo(enabled=False))
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        text = panel._status_text()
        assert "disabled" in text.lower()


# ── Close ─────────────────────────────────────────────────────────


async def test_escape_closes_panel() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        panel.focus()
        await pilot.press("escape")
        assert ("closed",) in app.captured


# ── Refresh in-place ──────────────────────────────────────────────


async def test_set_results_clears_expansion_and_resets_selection() -> None:
    """A new search batch should reset both selection and expansion —
    stale indices from the previous result set must not bleed into
    the new view."""
    app = _Host(results=[_hit("a"), _hit("b"), _hit("c")])
    async with app.run_test() as pilot:
        panel = app.query_one(KnowledgePanelWidget)
        panel.focus()
        await pilot.press("down", "down")
        await pilot.press("enter")  # expand index 2
        assert panel.selected_index == 2
        assert 2 in panel._expanded_indices
        panel.set_results([_hit("new")])
        assert panel.selected_index == 0
        assert panel._expanded_indices == set()


async def test_set_status_updates_header_in_place() -> None:
    app = _Host()
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        panel.set_status(
            KnowledgeStatusInfo(
                enabled=True,
                collection_name="changed",
                document_count=999,
                embedder="new-embedder",
            )
        )
        text = panel._status_text()
        assert "changed" in text
        assert "999" in text


# ── Busy indicator ────────────────────────────────────────────────


async def test_set_busy_swaps_status_text_and_restores() -> None:
    """The status line flips to the busy label while a search /
    ingest RPC is in-flight, then restores the static collection
    metadata once the awaited call's try/finally clears it.

    Without this, the panel looks frozen for the embed + ANN
    round-trip — there is no other in-panel signal that work is
    happening (the result rows don't change until results arrive).
    """
    app = _Host()
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)

        # Static state — collection metadata visible.
        assert "shared" in panel._status_text()
        assert "Searching" not in panel._status_text()

        panel.set_busy("Searching for 'auth'…")
        busy_text = panel._status_text()
        assert "Searching" in busy_text
        assert "'auth'" in busy_text
        # Static metadata is hidden behind the busy label so the
        # collection counter doesn't fight for visual space.
        assert "shared" not in busy_text

        panel.set_busy(None)
        assert "shared" in panel._status_text()
        assert "Searching" not in panel._status_text()


async def test_set_busy_empty_string_clears() -> None:
    """``set_busy("")`` is treated the same as ``set_busy(None)`` —
    callers passing an unguarded preview string can't accidentally
    leave the panel stuck on a blank label."""
    app = _Host()
    async with app.run_test():
        panel = app.query_one(KnowledgePanelWidget)
        panel.set_busy("Ingesting foo…")
        assert "Ingesting" in panel._status_text()
        panel.set_busy("")
        assert "Ingesting" not in panel._status_text()
        assert "shared" in panel._status_text()
