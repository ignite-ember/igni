"""MCP configuration — loads MCP server definitions and managed policies."""

import fnmatch
import json
import logging
import platform
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from ember_code.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)


class MCPTransport(str, Enum):
    """Supported MCP transport types."""

    stdio = "stdio"
    sse = "sse"


# Org-pushed MCP servers override the user-home and project-local roots.
#
# This used to be described as mirroring ``AgentPriority.ORG_GROUP``, and
# the two have deliberately diverged: agents, skills, commands and the
# rest now rank the *project* above the group, so a repository can
# override one by name. MCP servers stay group-first because they are
# closer to policy than preference — a server declaration names an
# endpoint and a command line, so letting a project shadow one would let
# it point an org-approved tool somewhere else.
#
# Not a number to keep in step with anything; it only has to stay above
# the project and user tiers here.
MCP_PRIORITY_GROUP = 5

# Stored file extension for per-server group policy MCP configs.
_GROUP_MCP_SUFFIX = ".json"


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    type: MCPTransport = MCPTransport.stdio
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    source_path: str = ""
    """Filesystem path of the .mcp.json file that defined this server."""
    source: str = "user"
    """Loader tier — ``"user"``, ``"project"``, ``"group-policy"``, or ``"plugin"``.
    Group-policy servers (org Group Policy) beat everything except managed-policy
    denials; see :data:`MCP_PRIORITY_GROUP`."""


class MCPPolicy(BaseModel):
    """Admin-controlled MCP server restrictions from managed settings."""

    required: list[str] = Field(default_factory=list)
    """Servers that MUST be connected."""
    allowed: list[str] = Field(default_factory=list)
    """Servers that CAN be connected (empty = all allowed)."""
    denied: list[str] = Field(default_factory=list)
    """Servers that are BLOCKED (supports glob patterns)."""

    def is_denied(self, server_name: str) -> bool:
        """Check if a server name matches any denied pattern."""
        return any(fnmatch.fnmatch(server_name, pat) for pat in self.denied)

    def is_allowed(self, server_name: str) -> bool:
        """Check if a server is allowed by the policy.

        A server is allowed when:
        - It is not denied, AND
        - The allowed list is empty (all allowed) or it appears in the list.
        """
        if self.is_denied(server_name):
            return False
        return not (self.allowed and server_name not in self.allowed)

    @classmethod
    def from_managed_settings(cls) -> "MCPPolicy":
        """Load MCP policy from managed settings (admin-controlled).

        Checks platform-specific managed settings paths:
        - macOS: /Library/Application Support/EmberCode/managed-settings.json
        - Linux: /etc/ignite-ember/managed-settings.json
        """
        system = platform.system()
        if system == "Darwin":
            path = Path("/Library/Application Support/EmberCode/managed-settings.json")
        elif system == "Linux":
            path = Path("/etc/ignite-ember/managed-settings.json")
        else:
            return cls()

        if not path.exists():
            return cls()

        try:
            with open(path) as f:
                data = json.load(f)
            mcp_data = data.get("mcp", {})
            return cls(**mcp_data)
        except (json.JSONDecodeError, OSError, TypeError):
            return cls()


class MCPConfigLoader:
    """Loads MCP server configurations from .mcp.json files."""

    def __init__(
        self,
        project_dir: Path | None = None,
        group_mcps_dir: Path | None = None,
    ):
        self.project_dir = project_dir or Path.cwd()
        # Optional: directory of per-server MCP overrides materialised by
        # :class:`GroupPolicyCache`. When set, each ``<name>.json`` file
        # is read with priority :data:`MCP_PRIORITY_GROUP` so org-pushed
        # servers override the user-home / project-local roots.
        self.group_mcps_dir = group_mcps_dir

    def load(self) -> dict[str, MCPServerConfig]:
        """Load MCP server configurations from all locations.

        Scans the three standard roots first (later writes win by
        re-assignment: project-local beats user-home), then layers the
        optional group-policy directory on top so ORG-pushed servers
        override everything except managed-policy denials.
        """
        servers: dict[str, MCPServerConfig] = {}

        paths = [
            Path.home() / CONFIG_DIR / ".mcp.json",
            self.project_dir / ".mcp.json",
            self.project_dir / CONFIG_DIR / ".mcp.json",
        ]

        for path in paths:
            self._load_from_file(path, servers)

        if self.group_mcps_dir is not None and self.group_mcps_dir.is_dir():
            for path in sorted(self.group_mcps_dir.glob(f"*{_GROUP_MCP_SUFFIX}")):
                self._load_from_file(path, servers, source="group-policy")

        return servers

    def _load_from_file(
        self,
        path: Path,
        servers: dict[str, MCPServerConfig],
        source: str = "user",
    ) -> None:
        """Load config from a single .mcp.json file."""
        if not path.exists():
            return

        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to parse MCP config %s: %s", path, exc)
            return

        mcp_servers = data.get("mcpServers", {})
        for name, config in mcp_servers.items():
            try:
                servers[name] = MCPServerConfig(
                    name=name,
                    type=config.get("type", "stdio"),
                    command=config.get("command", ""),
                    args=config.get("args", []),
                    env=config.get("env", {}),
                    url=config.get("url", ""),
                    source_path=str(path),
                    source=source,
                )
            except Exception as exc:
                logger.warning("MCP server '%s' in %s has invalid config: %s", name, path, exc)

    def load_plugin_servers(
        self,
        plugin_dir: Path,
        plugin_name: str,
        servers: dict[str, MCPServerConfig],
    ) -> None:
        """Merge a plugin's bundled ``.mcp.json`` into *servers*.

        Reads ``<plugin_dir>/.mcp.json`` (falling back to ``mcp.json``)
        and adds each bundled server with its name prefixed
        ``<plugin>:<server>`` so plugin servers can't collide with the
        user's own ``.mcp.json`` entries, and so the UI / status
        commands can attribute each connection back to its plugin.

        Collision policy: **first-wins**. If a server name already
        exists in *servers* (which would only happen for cross-plugin
        collisions on the prefixed name), the second occurrence is
        skipped and a warning logged. The user can resolve by
        disabling one of the plugins.
        """
        path = plugin_dir / ".mcp.json"
        if not path.is_file():
            path = plugin_dir / "mcp.json"
            if not path.is_file():
                return

        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse plugin MCP config %s: %s", path, exc)
            return

        mcp_servers = data.get("mcpServers", {})
        for raw_name, config in mcp_servers.items():
            prefixed = f"{plugin_name}:{raw_name}"
            if prefixed in servers:
                logger.warning(
                    "Plugin '%s' tried to register MCP server '%s' but the "
                    "prefixed name '%s' is already in use — keeping the "
                    "existing entry (first-wins).",
                    plugin_name,
                    raw_name,
                    prefixed,
                )
                continue
            try:
                servers[prefixed] = MCPServerConfig(
                    name=prefixed,
                    type=config.get("type", "stdio"),
                    command=config.get("command", ""),
                    args=config.get("args", []),
                    env=config.get("env", {}),
                    url=config.get("url", ""),
                    source_path=str(path),
                    source="plugin",
                )
            except Exception as exc:
                logger.warning(
                    "Plugin '%s' MCP server '%s' has invalid config: %s",
                    plugin_name,
                    raw_name,
                    exc,
                )
