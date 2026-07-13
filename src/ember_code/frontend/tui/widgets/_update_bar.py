"""Update bar — package-upgrade notification.

Extracted from ``_chrome.py`` (iter 35) per Pattern 8. The helper
``_upgrade_command`` detects the user's install method (Homebrew,
pipx, uv, or plain pip) so the displayed upgrade hint matches how
they actually installed the CLI.
"""

from __future__ import annotations

import subprocess
import sys

from textual.widgets import Static


def _upgrade_command(pkg_name: str) -> str:
    """Return the appropriate upgrade command based on install method."""
    exe = sys.executable

    # Check if running from a Homebrew prefix
    if "/Cellar/" in exe or "/homebrew/" in exe.lower():
        return f"brew upgrade {pkg_name}"

    # Check if pipx manages this package
    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and pkg_name in result.stdout:
            return f"pipx upgrade {pkg_name}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check if running inside a uv-managed environment
    if ".venv" in exe:
        try:
            subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                timeout=3,
            )
            return f"uv pip install --upgrade {pkg_name}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return f"pip install --upgrade {pkg_name}"


class UpdateBar(Static):
    """Top bar showing an available update notification."""

    DEFAULT_CSS = """
    UpdateBar {
        dock: bottom;
        height: 1;
        color: $warning;
        padding: 0 1;
    }

    UpdateBar.-hidden {
        display: none;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.add_class("-hidden")

    def show_update(self, current: str, latest: str, url: str = "", pkg_name: str = "") -> None:
        """Display an update notification."""
        msg = f"Update available: v{current} → v{latest}"
        if pkg_name:
            msg += f"  |  {_upgrade_command(pkg_name)}"
        self.update(msg)
        self.remove_class("-hidden")

    def hide(self) -> None:
        self.add_class("-hidden")
