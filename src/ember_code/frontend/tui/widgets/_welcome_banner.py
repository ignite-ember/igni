"""Welcome banner — shown once at TUI startup.

Extracted from ``_chrome.py`` (iter 35) per Pattern 8: small
modules, one responsibility.
"""

from __future__ import annotations

from textual.widgets import Static

from ember_code import __version__

_QUIT_KEY = "Ctrl+D"


class WelcomeBanner(Static):
    """Welcome banner shown at startup — minimal Claude Code style."""

    DEFAULT_CSS = """
    WelcomeBanner {
        padding: 1 0 0 0;
        margin: 0 0 1 0;
    }
    """

    def __init__(self):
        banner = (
            f"  [bold]igni[/bold] [dim]v{__version__}[/dim]\n"
            f"  [dim]/help for commands · {_QUIT_KEY} to quit[/dim]"
        )
        super().__init__(banner)
