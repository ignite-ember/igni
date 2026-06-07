"""Tests for ``LoopPanelWidget``.

The panel is a live status display for the active ``/loop`` plus
one verb (``X`` to cancel). Esc-handling lives outside the widget
(App-level priority binding → ``action_cancel``), so it's not
exercised here — see the corresponding CodeIndex-panel tests for
the same rationale.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from ember_code.frontend.tui.widgets._loop_panel import (
    LoopPanelWidget,
    LoopStatusInfo,
)

# ── Test harness ────────────────────────────────────────────────────


class _Host(App):
    def __init__(self, status: LoopStatusInfo | None = None) -> None:
        super().__init__()
        self._status = status or LoopStatusInfo(
            active=True,
            prompt="fix the typo in foo.py",
            iteration_index=3,
            iterations_remaining=7,
        )
        self.captured: list = []

    def compose(self) -> ComposeResult:
        yield LoopPanelWidget(status=self._status)

    def on_loop_panel_widget_cancel_requested(self, _m) -> None:
        self.captured.append(("cancel",))

    def on_loop_panel_widget_resume_requested(self, _m) -> None:
        self.captured.append(("resume",))

    def on_loop_panel_widget_panel_closed(self, _m) -> None:
        self.captured.append(("closed",))


# ── Status header ─────────────────────────────────────────────────


async def test_status_shows_running_and_progress_N_of_cap_when_explicit() -> None:
    """Explicit cap → render ``N / M``. ``N`` is the iterations
    *done*, ``M`` is the user-supplied cap; the row reads as
    "3 of 10 done"."""
    status = LoopStatusInfo(
        active=True,
        prompt="prompt",
        iteration_index=3,
        iterations_remaining=7,
        cap_explicit=True,
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "running" in text
        assert "3 / 10" in text


async def test_status_uses_announced_total_when_set() -> None:
    """Agent's announced total (via ``loop_set_total``) wins over
    the cap. The natural-language prompt rarely matches ``/loop N
    <prompt>`` syntax, so the model has to announce the actual
    count once it figures it out — this is the panel showing
    that announcement."""
    status = LoopStatusInfo(
        active=True,
        prompt="prompt",
        iteration_index=3,
        iterations_remaining=27,  # implicit safety net — would be hidden
        cap_explicit=False,
        announced_total=12,  # agent announced 12 items
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "running" in text
        # Renders ``3 / 12`` from the announcement, not ``3 / 30``
        # from the safety cap.
        assert "3 / 12" in text
        assert "30" not in text


async def test_announced_total_wins_over_explicit_cap() -> None:
    """When both are set (user typed ``/loop 30 ...`` but the
    agent later announced ``loop_set_total(12)``), the
    announcement reflects actual work and takes precedence."""
    status = LoopStatusInfo(
        active=True,
        prompt="prompt",
        iteration_index=3,
        iterations_remaining=27,
        cap_explicit=True,  # explicit cap of 30
        announced_total=12,  # but agent says 12 items
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "3 / 12" in text
        assert "30" not in text


async def test_status_hides_total_when_cap_is_implicit() -> None:
    """Implicit cap → the safety-net number is NOT a target, so
    the row shows only the current iteration. A "3/30" display
    would mislead the user into thinking the loop ends at 30,
    but on cap-hit it auto-extends to keep going."""
    status = LoopStatusInfo(
        active=True,
        prompt="prompt",
        iteration_index=3,
        iterations_remaining=27,
        cap_explicit=False,
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "running" in text
        assert "iteration 3" in text
        # No fake total — the safety-net number must not leak in.
        assert "/ 30" not in text
        assert "30" not in text


async def test_status_shows_empty_state_when_inactive() -> None:
    """No active loop → status surfaces the hint to start one. No
    progress counter, no prompt row."""
    status = LoopStatusInfo(active=False)
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "No active loop" in text
        # No iteration counter on the empty state.
        assert "/" not in text or "/loop" in text


async def test_prompt_row_renders_truncated_after_200_chars() -> None:
    """Long prompts get clipped at ~200 chars — the full prompt is
    in the slash-command history; this row only needs to confirm
    *which* loop is running."""
    long_prompt = "X" * 400
    status = LoopStatusInfo(active=True, prompt=long_prompt)
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        row = panel._prompt_text()
        assert "X" * 200 in row
        assert "X" * 201 not in row
        assert "…" in row


async def test_prompt_row_collapses_whitespace() -> None:
    """Multi-line / multi-space prompts collapse to a single line so
    the row doesn't blow up its height."""
    status = LoopStatusInfo(
        active=True,
        prompt="line one\n   line two\t\tline three",
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        row = panel._prompt_text()
        assert "\n" not in row
        assert "  " not in row.replace("prompt:", "").lstrip()


async def test_prompt_row_empty_when_inactive() -> None:
    """No active loop → no prompt row. (The empty-state hint lives
    in the status line, not duplicated here.)"""
    app = _Host(status=LoopStatusInfo(active=False))
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        assert panel._prompt_text() == ""


# ── Verb action ────────────────────────────────────────────────────


async def test_X_posts_cancel_when_loop_active() -> None:
    """``X`` fires ``CancelRequested`` while a loop is running."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        panel.focus()
        await pilot.press("X")
        assert ("cancel",) in app.captured


async def test_R_posts_resume_when_paused() -> None:
    """``R`` fires ``ResumeRequested`` only on a paused loop —
    the panel's primary action after a CLI restart."""
    app = _Host(
        status=LoopStatusInfo(
            active=True,
            paused=True,
            prompt="resume me",
            iteration_index=4,
            iterations_remaining=26,
        )
    )
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        panel.focus()
        await pilot.press("R")
        assert ("resume",) in app.captured


async def test_R_is_noop_when_running() -> None:
    """``R`` on an actively-pumping loop must not fire — there's
    nothing to resume, the loop is already firing iterations.
    Without this guard, a stray Shift-R would surface a
    confusing "Nothing to resume" toast mid-loop."""
    app = _Host(
        status=LoopStatusInfo(
            active=True, paused=False, prompt="p", iteration_index=2, iterations_remaining=8
        )
    )
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        panel.focus()
        await pilot.press("R")
        assert ("resume",) not in app.captured


async def test_R_is_noop_when_inactive() -> None:
    """``R`` on an empty panel (no loop at all) must not fire —
    same rationale as the running case."""
    app = _Host(status=LoopStatusInfo(active=False))
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        panel.focus()
        await pilot.press("R")
        assert ("resume",) not in app.captured


async def test_status_shows_paused_yellow_with_progress() -> None:
    """Paused loop renders a yellow badge instead of the green
    running one. The counter still shows N/M so the user knows
    where they were interrupted."""
    status = LoopStatusInfo(
        active=True, paused=True, prompt="p", iteration_index=4, iterations_remaining=26
    )
    app = _Host(status=status)
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        text = panel._status_text()
        assert "[yellow]" in text
        assert "paused" in text
        assert "4 / 30" in text
        # No green "running" badge bleeding through.
        assert "running" not in text


async def test_paused_hint_surfaces_R_resume_first() -> None:
    """The hint changes when paused — ``R resume`` is the primary
    action after a restart, so it leads."""
    paused = _Host(
        status=LoopStatusInfo(
            active=True, paused=True, prompt="p", iteration_index=4, iterations_remaining=26
        )
    )
    async with paused.run_test():
        panel = paused.query_one(LoopPanelWidget)
        hint = panel._hint_text()
        assert "R resume" in hint
        # Cancel still available — both options visible.
        assert "X cancel" in hint


async def test_X_is_noop_when_inactive() -> None:
    """``X`` against an empty panel must not post ``CancelRequested``
    — otherwise the app would surface a confusing "Nothing to
    cancel" message every time the user pressed a stray key."""
    app = _Host(status=LoopStatusInfo(active=False))
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        panel.focus()
        await pilot.press("X")
        assert ("cancel",) not in app.captured


# ── Busy indicator ────────────────────────────────────────────────


async def test_set_busy_swaps_status_and_hides_prompt() -> None:
    """Busy label takes over both the status and prompt rows so the
    user sees a single spinner-like indicator instead of stale
    "running 3/10" text mid-cancel."""
    app = _Host()
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)

        idle = panel._status_text()
        assert "running" in idle
        assert "fix the typo" in panel._prompt_text()

        panel.set_busy("Cancelling loop…")
        busy = panel._status_text()
        assert "Cancelling" in busy
        assert "running" not in busy
        # Prompt row is suppressed during busy so the panel reads as
        # a single in-flight indicator.
        assert panel._prompt_text() == ""

        panel.set_busy(None)
        assert "running" in panel._status_text()
        assert "fix the typo" in panel._prompt_text()


async def test_set_busy_empty_string_clears() -> None:
    app = _Host()
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        panel.set_busy("Cancelling…")
        assert "Cancelling" in panel._status_text()
        panel.set_busy("")
        assert "Cancelling" not in panel._status_text()


# ── Live refresh ──────────────────────────────────────────────────


async def test_set_status_updates_counter_in_place() -> None:
    """``set_status`` is what the 1s app poll calls — verify it
    actually updates the iteration counter mid-life (the whole
    point of the panel)."""
    app = _Host(
        status=LoopStatusInfo(
            active=True,
            prompt="prompt",
            iteration_index=0,
            iterations_remaining=10,
            cap_explicit=True,
        )
    )
    async with app.run_test():
        panel = app.query_one(LoopPanelWidget)
        assert "0 / 10" in panel._status_text()
        panel.set_status(
            LoopStatusInfo(
                active=True,
                prompt="prompt",
                iteration_index=5,
                iterations_remaining=5,
                cap_explicit=True,
            )
        )
        assert "5 / 10" in panel._status_text()


# ── Close ─────────────────────────────────────────────────────────


async def test_unmount_posts_panel_closed() -> None:
    """``PanelClosed`` fires on unmount regardless of removal path.
    Same rationale as the CodeIndex panel — App's priority Esc
    binding removes via ``action_cancel``, not the widget's
    ``on_key``, so ``on_unmount`` is the only reliable hook for
    "panel is going away" cleanup."""
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(LoopPanelWidget)
        await panel.remove()
        await pilot.pause()
        assert ("closed",) in app.captured
