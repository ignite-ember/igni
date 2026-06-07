"""Loop panel widget — live status of the active ``/loop``.

The ``/loop`` state is tiny (one prompt + an iteration counter +
a remaining-cap counter) but mid-loop it churns every few seconds
as each iteration completes. The panel surfaces:

  * The prompt being re-fired each iteration (truncated to keep
    rows scannable).
  * Iteration progress: ``N / M`` where ``N`` is the count of
    iterations already fired and ``M`` is the configured cap.
  * One verb action: ``X`` cancels the loop (RPCs
    ``cancel_pending_loop``).

When no loop is active, the panel shows an empty-state hint and
``X`` is a no-op. The app handler polls ``loop_status`` every
~1s while the panel is open; faster than CodeIndex's 2s because
iterations fire on idle and the user is watching the counter
tick.
"""

from __future__ import annotations

import contextlib
import logging

from pydantic import BaseModel
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

logger = logging.getLogger(__name__)


__all__ = [
    "LoopPanelWidget",
    "LoopStatusInfo",
]


# ── View models ────────────────────────────────────────────────────


class LoopStatusInfo(BaseModel):
    """Panel-side view of the ``/loop`` state.

    Mirrors :py:meth:`BackendServer.loop_status` exactly — the
    backend builds this dict; the panel reconstructs.

    ``paused`` is True for a loop loaded from disk on startup that
    hasn't been resumed yet — distinguishes "interrupted, waiting
    for ``R``" from "actively pumping iterations".
    """

    active: bool = False
    paused: bool = False
    prompt: str = ""
    iteration_index: int = 0
    iterations_remaining: int = 0
    # True when the user explicitly capped the run
    # (``/loop N <prompt>``) — the panel then shows ``N / M``. False
    # for the default safety-net cap; the panel hides the total
    # since there isn't a meaningful one.
    cap_explicit: bool = False
    # The agent's announced iteration total via ``loop_set_total``.
    # When set, the panel renders ``N / announced_total`` — this
    # takes precedence over ``cap_explicit`` because the agent's
    # count reflects the actual work, not just a safety bound.
    announced_total: int | None = None


# ── Widget ─────────────────────────────────────────────────────────


class LoopPanelWidget(Widget):
    """Bottom-docked panel — live status of the active ``/loop``."""

    can_focus = True

    DEFAULT_CSS = """
    LoopPanelWidget {
        layer: dialog;
        dock: bottom;
        width: 100%;
        height: auto;
        background: $surface-darken-1;
        border-top: heavy $accent;
        padding: 0 2;
    }

    LoopPanelWidget .lp-title {
        text-style: bold;
        color: $accent;
    }

    LoopPanelWidget .lp-status {
        color: $text-muted;
        margin-bottom: 1;
    }

    LoopPanelWidget .lp-prompt {
        color: $text;
        margin-bottom: 1;
    }

    LoopPanelWidget .hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    # ── Outbound messages ────────────────────────────────────────────

    class CancelRequested(Message):
        pass

    class ResumeRequested(Message):
        pass

    class PanelClosed(Message):
        pass

    def __init__(self, status: LoopStatusInfo):
        super().__init__()
        self._status = status
        self._busy_label: str | None = None

    def on_mount(self) -> None:
        """Self-focus after mount so ``on_key`` reliably picks
        up ``X``. See ``CodeIndexPanelWidget.on_mount`` for the
        broader rationale (no focusable children, Esc consumed
        by the App-level priority binding)."""
        self.focus()

    def on_unmount(self) -> None:
        """Post ``PanelClosed`` regardless of removal path so
        the app can stop the status-poll interval. ``action_cancel``
        in the App calls ``widget.remove()`` directly on Esc, so
        the on_key escape branch alone wouldn't catch every close.

        Posted via ``self.app`` — once the widget is mid-detach
        its own pump no longer bubbles to the App. See the
        matching note in CodeIndex panel."""
        with contextlib.suppress(Exception):
            self.app.post_message(self.PanelClosed())

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("[bold $accent]Loop[/bold $accent]", classes="lp-title")
        yield Static(self._status_text(), classes="lp-status")
        yield Static(self._prompt_text(), classes="lp-prompt")
        yield Static(self._hint_text(), classes="hint")

    # ── Render helpers ──────────────────────────────────────────────

    def _status_text(self) -> str:
        if self._busy_label:
            return f"[bold $accent]{self._busy_label}[/bold $accent]"
        s = self._status
        if not s.active:
            return "[dim]No active loop. Start one with `/loop <prompt>`.[/dim]"
        # ``N / M`` where N is the iterations *done* and M is the
        # configured cap (done + remaining). The user wants to see
        # the cap, not the gap — "3/30" reads faster than
        # "3 done, 27 remaining".
        # Paused = state loaded from disk after a restart, no
        # iteration is firing. Yellow rather than green so the user
        # knows the counter isn't ticking — they need to hit ``R``.
        badge = "[yellow]paused[/yellow]" if s.paused else "[green]running[/green]"
        # Priority for the "total" half of ``N / M``:
        #
        # 1. ``announced_total`` — the agent called ``loop_set_total``
        #    with the actual count it derived from the work
        #    (e.g. after listing the files to process). This is
        #    the most truthful number — show it.
        # 2. ``cap_explicit`` — the user typed ``/loop N <prompt>``,
        #    so ``N`` is the intended total. Show it.
        # 3. ``paused`` — the loop was interrupted; the user wants
        #    to see where they were, so surface ``N / (N+remaining)``
        #    even when the cap is an implicit safety net.
        # 4. Otherwise — the cap is a safety net only. Show just
        #    the iteration number; auto-extend keeps going past
        #    the original batch so any ``M`` we display would
        #    mislead.
        if s.announced_total is not None and s.announced_total > 0:
            progress = f"[bold]{s.iteration_index} / {s.announced_total}[/bold] iterations"
        elif s.cap_explicit or s.paused:
            cap = s.iteration_index + s.iterations_remaining
            progress = f"[bold]{s.iteration_index} / {cap}[/bold] iterations"
        else:
            progress = f"[bold]iteration {s.iteration_index}[/bold]"
        return f"{badge}  [dim]·[/dim]  {progress}"

    def _prompt_text(self) -> str:
        if self._busy_label or not self._status.active:
            return ""
        # Keep the prompt scannable — long prompts get clipped at
        # ~200 chars. The full prompt is recoverable from the slash
        # command history; this row is meant to confirm *which*
        # loop is running, not surface the whole prompt.
        prompt = " ".join((self._status.prompt or "").split())
        if len(prompt) > 200:
            prompt = prompt[:200] + "…"
        return f"[dim]prompt:[/dim] {prompt}"

    def _hint_text(self) -> str:
        if not self._status.active:
            return "[dim]Esc close[/dim]"
        # Paused loop surfaces ``R resume`` first because that's the
        # primary action — the user opened the panel post-restart
        # specifically to either continue or kill the interrupted run.
        if self._status.paused:
            return "[dim]R resume · X cancel loop · Esc close[/dim]"
        return "[dim]X cancel loop · Esc close[/dim]"

    # ── Refresh / rebuild ─────────────────────────────────────────

    def set_status(self, status: LoopStatusInfo) -> None:
        """Replace the header. Called by the app's 1s poll so the
        iteration counter ticks live as each iteration fires."""
        self._status = status
        with contextlib.suppress(Exception):
            self.query_one(".lp-status", Static).update(self._status_text())
        with contextlib.suppress(Exception):
            self.query_one(".lp-prompt", Static).update(self._prompt_text())
        with contextlib.suppress(Exception):
            self.query_one(".hint", Static).update(self._hint_text())

    def set_busy(self, label: str | None) -> None:
        """Flip the status line to a busy indicator. Pair in a
        try/finally around the RPC so a failure doesn't leave the
        panel stuck spinning."""
        self._busy_label = label or None
        with contextlib.suppress(Exception):
            self.query_one(".lp-status", Static).update(self._status_text())
        with contextlib.suppress(Exception):
            self.query_one(".lp-prompt", Static).update(self._prompt_text())

    # ── Input ─────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        # Esc is intentionally not handled here — the App's
        # ``priority=True`` escape binding fires first and routes
        # through ``action_cancel`` (which removes the widget by
        # type). Re-handling it here would double-fire
        # ``PanelClosed`` (once from this branch, once from
        # ``on_unmount`` during the removal).
        if event.key == "X":
            event.stop()
            event.prevent_default()
            # Only fires when a loop is actually running — silent
            # no-op otherwise so a stray keypress doesn't surface
            # a confusing "Nothing to cancel" message.
            if self._status.active:
                self.post_message(self.CancelRequested())
            return
        if event.key == "R":
            event.stop()
            event.prevent_default()
            # ``R`` only makes sense on a paused loop. Running and
            # empty states both no-op so a stray Shift-R doesn't
            # produce a misleading "Nothing to resume" toast.
            if self._status.active and self._status.paused:
                self.post_message(self.ResumeRequested())
