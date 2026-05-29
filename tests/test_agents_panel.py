"""Tests for ``AgentsPanelWidget``.

Same shape as the plugins-panel tests: drive the widget through
``App.run_test()`` and assert on the message-bus contract (which
keys post which events) plus the render-helper branches that change
behavior. Backend interactions (promote / discard side effects) are
covered by the backend tests.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._agents_panel import (
    AgentInfo,
    AgentsPanelWidget,
)

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(self, agents: list[AgentInfo]) -> None:
        super().__init__()
        self._agents = agents
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield AgentsPanelWidget(agents=self._agents)

    def on_agents_panel_widget_promote_requested(self, m) -> None:
        self.captured.append(("promote", m.name))

    def on_agents_panel_widget_discard_requested(self, m) -> None:
        self.captured.append(("discard", m.name))

    def on_agents_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


def _agent(name: str, **kw) -> AgentInfo:
    return AgentInfo(name=name, **kw)


# ── Key bindings ────────────────────────────────────────────────────


async def test_arrow_keys_navigate() -> None:
    """↑/↓ move ``selected_index`` and clamp at the list bounds —
    re-renders depend on the value staying in range."""
    app = _Host([_agent("a"), _agent("b"), _agent("c")])
    async with app.run_test() as pilot:
        panel = app.query_one(AgentsPanelWidget)
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        # Clamp at end.
        for _ in range(5):
            await pilot.press("down")
        assert panel.selected_index == 2
        await pilot.press("up", "up", "up", "up")
        assert panel.selected_index == 0


async def test_p_promotes_ephemeral_agent() -> None:
    """`p` on an ephemeral agent posts ``PromoteRequested``. Regular
    agents are filtered out so a stray ``p`` press on a permanent
    agent doesn't surface a noisy backend error."""
    app = _Host([_agent("alpha", is_ephemeral=True)])
    async with app.run_test() as pilot:
        await pilot.press("p")
        assert ("promote", "alpha") in app.captured


async def test_p_on_permanent_agent_is_noop() -> None:
    app = _Host([_agent("alpha", is_ephemeral=False)])
    async with app.run_test() as pilot:
        await pilot.press("p")
        assert app.captured == []


async def test_d_discards_ephemeral_agent() -> None:
    app = _Host([_agent("alpha", is_ephemeral=True)])
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert ("discard", "alpha") in app.captured


async def test_d_on_permanent_agent_is_noop() -> None:
    app = _Host([_agent("alpha", is_ephemeral=False)])
    async with app.run_test() as pilot:
        await pilot.press("d")
        assert app.captured == []


async def test_enter_toggles_detail_expansion() -> None:
    """Enter on the selected row toggles the expanded detail view.
    Second Enter collapses back. State lives on the panel
    (``_expanded_indices``) so re-renders preserve it."""
    app = _Host([_agent("alpha", system_prompt="Detailed system prompt body.")])
    async with app.run_test() as pilot:
        panel = app.query_one(AgentsPanelWidget)
        assert 0 not in panel._expanded_indices
        await pilot.press("enter")
        assert 0 in panel._expanded_indices
        await pilot.press("enter")
        assert 0 not in panel._expanded_indices


async def test_escape_closes_panel() -> None:
    app = _Host([_agent("alpha")])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


async def test_escape_works_when_empty() -> None:
    app = _Host([])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


# ── Render helpers — format-affecting branches ──────────────────────


async def test_entry_renders_name_model_and_tools_count() -> None:
    app = _Host([_agent("alpha", model="gpt-5", tools=["Read", "Edit"])])
    async with app.run_test():
        panel = app.query_one(AgentsPanelWidget)
        rendered = panel._render_entry(panel._agents[0])
        assert "alpha" in rendered
        assert "gpt-5" in rendered
        assert "2 tool" in rendered


async def test_entry_marks_ephemeral() -> None:
    """Ephemeral agents render with an ``(ephemeral)`` marker so the
    user knows ``p`` / ``d`` are available — without it, the only
    affordance signal would be the help line at the bottom."""
    app = _Host([_agent("alpha", is_ephemeral=True), _agent("beta")])
    async with app.run_test():
        panel = app.query_one(AgentsPanelWidget)
        eph = panel._render_entry(panel._agents[0])
        perm = panel._render_entry(panel._agents[1])
        assert "ephemeral" in eph.lower()
        assert "ephemeral" not in perm.lower()


async def test_expanded_shows_system_prompt_preview() -> None:
    """Expanded view truncates the system prompt at 240 chars +
    ellipsis. Full prompt is a Read away from the source file —
    the panel just gives enough to recognize the agent's role."""
    long_prompt = "A" * 500
    app = _Host([_agent("alpha", system_prompt=long_prompt)])
    async with app.run_test():
        panel = app.query_one(AgentsPanelWidget)
        expanded = panel._render_entry_expanded(panel._agents[0])
        assert "Prompt:" in expanded
        # Truncated with ellipsis.
        assert "…" in expanded
        assert len(expanded) < len(long_prompt) + 500


async def test_title_counts_ephemeral_subset() -> None:
    """Title shows total loaded + ephemeral subset count."""
    app = _Host(
        [
            _agent("a", is_ephemeral=False),
            _agent("b", is_ephemeral=True),
            _agent("c", is_ephemeral=True),
        ]
    )
    async with app.run_test():
        panel = app.query_one(AgentsPanelWidget)
        title = panel._title_text()
        assert "3 loaded" in title
        assert "2 ephemeral" in title


# ── Refresh in-place ────────────────────────────────────────────────


async def test_refresh_agents_clamps_selection_on_shrink() -> None:
    """``refresh_agents`` after the list shrinks clamps the selection
    to the new bounds — same contract as the plugins panel's
    ``refresh_data``."""
    app = _Host([_agent("a"), _agent("b"), _agent("c")])
    async with app.run_test() as pilot:
        panel = app.query_one(AgentsPanelWidget)
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        panel.refresh_agents([_agent("a")])
        await pilot.pause()
        assert panel.selected_index == 0
