"""Tests for ``SkillsPanelWidget``.

Same harness as the agents/plugins panels — Textual ``App.run_test()``
drives real key events; we assert on the message-bus contract and
the render-helper branches that change behavior.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._skills_panel import SkillInfo, SkillsPanelWidget

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(self, skills: list[SkillInfo]) -> None:
        super().__init__()
        self._skills = skills
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield SkillsPanelWidget(skills=self._skills)

    def on_skills_panel_widget_run_requested(self, m) -> None:
        self.captured.append(("run", m.name))

    def on_skills_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


def _skill(name: str, **kw) -> SkillInfo:
    return SkillInfo(name=name, **kw)


# ── Key bindings ────────────────────────────────────────────────────


async def test_arrow_keys_navigate() -> None:
    """↑/↓ move selection and clamp at the list bounds — re-renders
    depend on the value staying in range."""
    app = _Host([_skill("a"), _skill("b"), _skill("c")])
    async with app.run_test() as pilot:
        panel = app.query_one(SkillsPanelWidget)
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        for _ in range(5):
            await pilot.press("down")
        assert panel.selected_index == 2
        for _ in range(5):
            await pilot.press("up")
        assert panel.selected_index == 0


async def test_r_emits_run_for_user_invocable() -> None:
    """`r` on a user-invocable skill posts ``RunRequested``."""
    app = _Host([_skill("commit", user_invocable=True)])
    async with app.run_test() as pilot:
        await pilot.press("r")
        assert ("run", "commit") in app.captured


async def test_r_on_agent_only_skill_is_noop() -> None:
    """Skills marked ``user_invocable: false`` are agent-only —
    `r` from the panel must NOT surface them as runnable. Otherwise
    we'd dispatch a slash command the user can't normally type, and
    the backend would either error or silently misbehave."""
    app = _Host([_skill("internal", user_invocable=False)])
    async with app.run_test() as pilot:
        await pilot.press("r")
        assert app.captured == []


async def test_enter_toggles_detail_expansion() -> None:
    """Enter on the selected row toggles the expanded detail view."""
    app = _Host([_skill("commit", body="Stage and commit changes.")])
    async with app.run_test() as pilot:
        panel = app.query_one(SkillsPanelWidget)
        assert 0 not in panel._expanded_indices
        await pilot.press("enter")
        assert 0 in panel._expanded_indices
        await pilot.press("enter")
        assert 0 not in panel._expanded_indices


async def test_escape_closes_panel() -> None:
    app = _Host([_skill("a")])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


async def test_escape_works_when_empty() -> None:
    app = _Host([])
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert ("closed",) in app.captured


# ── Render helpers ──────────────────────────────────────────────────


async def test_entry_renders_name_with_slash_prefix() -> None:
    """Entries lead with ``/`` to match how the user invokes them.
    Without the slash the panel would visually disconnect from the
    typed command surface."""
    app = _Host([_skill("commit")])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        rendered = panel._render_entry(panel._skills[0])
        assert "/commit" in rendered


async def test_entry_shows_argument_hint() -> None:
    app = _Host([_skill("deploy", argument_hint="[environment]")])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        rendered = panel._render_entry(panel._skills[0])
        assert "[environment]" in rendered


async def test_entry_description_clipped_at_160_with_ellipsis() -> None:
    """Collapsed-row descriptions are clipped at 160 chars and end
    with ``...``. The budget was 80 — doubled so users get a real
    summary at a glance, not just a teaser. Descriptions that fit
    inside the budget are rendered verbatim with no trailing dots."""
    long_desc = "A" * 200
    short_desc = "Run the test suite and report results."
    app = _Host([_skill("long", description=long_desc), _skill("short", description=short_desc)])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        long_rendered = panel._render_entry(panel._skills[0])
        short_rendered = panel._render_entry(panel._skills[1])

        # Long is clipped at 160 chars and ends with "..."
        assert "A" * 160 in long_rendered
        assert "A" * 161 not in long_rendered
        assert "..." in long_rendered

        # Short is rendered verbatim — no clipping artifact.
        assert short_desc in short_rendered
        assert "..." not in short_rendered


async def test_entry_marks_agent_only() -> None:
    """Skills with ``user_invocable: false`` carry an ``(agent-only)``
    badge so users know `r` won't run them from the panel."""
    app = _Host([_skill("hidden", user_invocable=False), _skill("normal")])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        hidden = panel._render_entry(panel._skills[0])
        normal = panel._render_entry(panel._skills[1])
        assert "agent-only" in hidden.lower()
        assert "agent-only" not in normal.lower()


async def test_expanded_shows_full_body() -> None:
    """Expanded view shows the full body verbatim — no truncation,
    no ellipsis. Skills are short by construction (prompt templates,
    not docs), and the panel's internal scroll handles overflow."""
    body = (
        "Stage and commit changes.\n\n"
        "## Steps\n"
        "1. Run the test suite — abort if any tests fail\n"
        "2. Build the application\n"
        "3. Deploy to the target environment"
    )
    app = _Host([_skill("commit", body=body)])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        expanded = panel._render_entry_expanded(panel._skills[0])
        assert "Body:" in expanded
        # Full body present — no ellipsis, no clip.
        assert "abort if any tests fail" in expanded
        assert "target environment" in expanded
        assert "…" not in expanded


async def test_expanded_renders_full_long_body() -> None:
    """A multi-hundred-char body lands verbatim. Previously head-
    clipped at 240 chars — that's gone so users see the whole
    prompt template, not a teaser."""
    long_body = "B" * 800
    app = _Host([_skill("long", body=long_body)])
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        expanded = panel._render_entry_expanded(panel._skills[0])
        # Full 800 B's present.
        assert "B" * 800 in expanded
        assert "…" not in expanded


async def test_title_counts_unique_categories() -> None:
    """Title shows total loaded + unique category count. Categories
    de-duplicate so a category with many skills still counts as one."""
    app = _Host(
        [
            _skill("a", category="development"),
            _skill("b", category="development"),
            _skill("c", category="review"),
        ]
    )
    async with app.run_test():
        panel = app.query_one(SkillsPanelWidget)
        title = panel._title_text()
        assert "3 loaded" in title
        assert "2 categor" in title  # "2 categories"


# ── Refresh in-place ────────────────────────────────────────────────


async def test_refresh_skills_clamps_selection() -> None:
    app = _Host([_skill("a"), _skill("b"), _skill("c")])
    async with app.run_test() as pilot:
        panel = app.query_one(SkillsPanelWidget)
        await pilot.press("down", "down")
        assert panel.selected_index == 2
        panel.refresh_skills([_skill("a")])
        await pilot.pause()
        assert panel.selected_index == 0
