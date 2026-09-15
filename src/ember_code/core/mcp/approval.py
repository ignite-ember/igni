"""MCP first-use approval — prompts before connecting project-scoped servers."""

import json
import logging
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from ember_code.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

# User-global config path — servers from here are auto-approved.
_USER_GLOBAL_MCP = str(Path.home() / CONFIG_DIR / ".mcp.json")


class MCPApprovalManager:
    """Manages first-use approval for project-scoped MCP servers.

    Approved servers are persisted to ``~/.igni/mcp-approved.json`` so the
    prompt only appears once per (server_name, config_path) pair.  Servers
    defined in the user-global config (``~/.igni/.mcp.json``) are trusted
    automatically and never prompt.
    """

    def __init__(self, approval_path: Path | None = None) -> None:
        self._path = approval_path or (Path.home() / CONFIG_DIR / "mcp-approved.json")
        self._approved: dict[str, list[str]] = self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_approved(self, server_name: str, config_path: str) -> bool:
        """Return True if *server_name* from *config_path* is already approved."""
        if self._is_user_global(config_path):
            return True
        if self._is_project_config(config_path):
            return True  # project .mcp.json is trusted by default
        return config_path in self._approved.get(server_name, [])

    def approve(self, server_name: str, config_path: str) -> None:
        """Mark *server_name* from *config_path* as approved and persist."""
        sources = self._approved.setdefault(server_name, [])
        if config_path not in sources:
            sources.append(config_path)
            self._save()

    def check_approval(self, server_name: str, config_path: str) -> bool:
        """Interactive check — prompt the user if the server is not yet approved.

        Returns True when the server is (or becomes) approved, False if the
        user declines.
        """
        if self.is_approved(server_name, config_path):
            return True

        console = Console(stderr=True)
        console.print(
            f"\n[bold yellow]MCP server '[cyan]{server_name}[/cyan]' "
            f"wants to connect.[/bold yellow]"
        )
        console.print(f"  Source: [dim]{config_path}[/dim]")

        granted = Confirm.ask(
            "  Allow this MCP server?",
            default=False,
            console=console,
        )

        if granted:
            self.approve(server_name, config_path)
        return granted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_user_global(config_path: str) -> bool:
        """Check whether *config_path* is the user-global MCP config."""
        try:
            return str(Path(config_path).resolve()) == str(Path(_USER_GLOBAL_MCP).resolve())
        except (OSError, ValueError):
            return config_path == _USER_GLOBAL_MCP

    @staticmethod
    def _is_project_config(config_path: str) -> bool:
        """Check whether *config_path* is a standard project MCP config."""
        name = Path(config_path).name
        return name in (".mcp.json", "mcp.json")

    def _load(self) -> dict[str, list[str]]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._approved, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to save MCP approvals: %s", exc)
