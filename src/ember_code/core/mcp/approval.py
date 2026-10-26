"""MCP first-use approval — prompts before connecting project-scoped servers."""

import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from ember_code.core.config.group_policy import group_policy_dir
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.profile import RuntimeProfile

logger = logging.getLogger(__name__)

# User-global config path — servers from here are auto-approved.
_USER_GLOBAL_MCP = str(Path.home() / CONFIG_DIR / ".mcp.json")


class MCPApprovalManager:
    """Manages first-use approval for project-scoped MCP servers.

    Approved servers are persisted to ``~/.igni/mcp-approved.json`` so the
    prompt only appears once per (server_name, config_path) pair.  Servers
    defined in the user-global config (``~/.igni/.mcp.json``) are trusted
    automatically and never prompt.

    Every other source — the project's ``.mcp.json``, a group-policy
    config, a plugin's bundled ``.mcp.json`` — prompts on first use.
    ``.mcp.json`` is a file anyone can commit to a public repo or ship
    in a plugin, and connecting a stdio server means spawning its
    ``command`` locally as the user; cloning a repo and sending one
    message must not be enough to run it.

    **In the cloud** (``IGNI_PROFILE=cloud``) approvals come from igni
    server and are read-only in the VM: a server is approved when igni
    server delivered it — a group policy's ``mcps`` entry, which an
    administrator configured — and no other is, nor can be approved
    from inside the VM. Nothing is read from or written to
    ``mcp-approved.json``.
    """

    def __init__(
        self, approval_path: Path | None = None, delivered_dir: Path | None = None
    ) -> None:
        self._path = approval_path or (Path.home() / CONFIG_DIR / "mcp-approved.json")
        self._cloud = RuntimeProfile.current().is_cloud
        #: Where igni server's group policy delivers MCP configs.
        self._delivered = delivered_dir or (group_policy_dir() / "mcps")
        self._approved: dict[str, list[str]] = {} if self._cloud else self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_approved(self, server_name: str, config_path: str) -> bool:
        """Return True if *server_name* from *config_path* is already approved."""
        if self._cloud:
            return self._is_delivered(config_path)
        if self._is_user_global(config_path):
            return True
        # No basename-based trust here. Matching on ``.mcp.json`` /
        # ``mcp.json`` auto-approved every project and every plugin
        # bundle, which made the prompt below unreachable for the two
        # sources it exists to gate. Only a recorded approval counts.
        return config_path in self._approved.get(server_name, [])

    def approve(self, server_name: str, config_path: str) -> None:
        """Mark *server_name* from *config_path* as approved and persist."""
        if self._cloud:
            logger.warning(
                "MCP approvals come from igni server in the cloud; not approving '%s'",
                server_name,
            )
            return
        sources = self._approved.setdefault(server_name, [])
        if config_path not in sources:
            sources.append(config_path)
            self._save()

    def check_approval(self, server_name: str, config_path: str) -> bool:
        """Interactive check — prompt the user if the server is not yet approved.

        Returns True when the server is (or becomes) approved, False if the
        user declines or there is nobody to ask.
        """
        if self.is_approved(server_name, config_path):
            return True
        if self._cloud:
            logger.warning(
                "MCP '%s' from %s is not one igni server delivered — not connecting.",
                server_name,
                config_path,
            )
            return False

        if not self._can_prompt():
            # Fail closed. The auto-connect pass runs on the backend's
            # event loop (``session/startup/mcp.py`` connects every
            # configured server on the first message) where stdin is not
            # a terminal: ``Confirm.ask`` would either raise EOFError or
            # block the loop — and the whole point of this gate is that
            # "we could not ask" must never mean "yes".
            logger.warning(
                "MCP '%s' from %s needs first-use approval and there is no "
                "interactive terminal to ask on — not connecting. Approve it "
                "once from an interactive igni session, or add the path under "
                "'%s' in %s.",
                server_name,
                config_path,
                server_name,
                self._path,
            )
            return False

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

    def _is_delivered(self, config_path: str) -> bool:
        """Whether igni server's group policy delivered *config_path*."""
        try:
            return Path(config_path).resolve().is_relative_to(self._delivered.resolve())
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_user_global(config_path: str) -> bool:
        """Check whether *config_path* is the user-global MCP config."""
        try:
            return str(Path(config_path).resolve()) == str(Path(_USER_GLOBAL_MCP).resolve())
        except (OSError, ValueError):
            return config_path == _USER_GLOBAL_MCP

    @staticmethod
    def _can_prompt() -> bool:
        """Whether there is a human on a terminal to answer the prompt.

        ``sys.stdin`` can be ``None`` (pythonw / packaged GUI builds)
        and ``isatty()`` raises on a closed stream, so both are treated
        as "cannot ask".
        """
        try:
            return bool(sys.stdin is not None and sys.stdin.isatty())
        except (AttributeError, OSError, ValueError):
            return False

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
