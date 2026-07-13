"""Tip bar — one-line usage hint above the composer.

Extracted from ``_chrome.py`` (iter 35) per Pattern 8: small
modules, one responsibility.
"""

from __future__ import annotations

from textual.widgets import Static


class TipBar(Static):
    """Usage-tip bar. Host app decides where it sits.

    Originally ``dock: bottom``, but the EmberApp now nests TipBar
    inside the ``#footer`` Vertical container (along with prompt-row
    and status-bar) so a single dock-bottom anchor handles all the
    chrome. Two dock-bottom siblings would overlap rather than
    stack, which broke status-bar visibility on mid-session resize.
    """

    DEFAULT_CSS = """
    TipBar {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, tip: str | None = None, **kwargs):
        self._tip = tip or ""
        display = f"[dim italic]Tip: {self._tip}[/dim italic]" if self._tip else ""
        super().__init__(display, **kwargs)

    def set_tip(self, tip: str) -> None:
        """Update the displayed tip."""
        self._tip = tip
        self.update(f"[dim italic]Tip: {tip}[/dim italic]")
