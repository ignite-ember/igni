"""Per-project persistence for the "disabled MCP tools" list.

Extracted from :mod:`ember_code.core.mcp.client` so the on-disk
representation (``.ember/mcp-tool-state.json``) has a single owner
and the ``MCPClientManager`` can focus on transport / connection
lifecycle. The state is a plain ``{server: set[tool_name]}``
mapping; the file is a JSON blob keyed under ``"disabled"``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class MCPToolStateStore:
    """File-backed store for the per-project disabled-tools list.

    Lives at ``<project>/.ember/mcp-tool-state.json``. When
    ``project_dir`` is ``None`` (headless / test) the store is a
    no-op — ``load`` returns an empty dict and ``save`` silently
    drops. That mirrors the manager's behaviour before this class
    existed.
    """

    def __init__(self, project_dir: Path | None):
        self._project_dir = project_dir

    def path(self) -> Path | None:
        if self._project_dir is None:
            return None
        return self._project_dir / ".ember" / "mcp-tool-state.json"

    def load(self) -> dict[str, set[str]]:
        """Return ``{server: set[tool_name]}`` from disk. Empty when
        the file is missing, unreadable, or malformed — the store
        degrades to "no disabled tools" rather than raising."""
        path = self.path()
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text() or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("MCP tool state read failed (%s): %s", path, exc)
            return {}
        out: dict[str, set[str]] = {}
        for server, tools in (data.get("disabled") or {}).items():
            if isinstance(tools, list):
                out[server] = {str(t) for t in tools}
        return out

    def save(self, disabled: dict[str, set[str]]) -> None:
        """Write the state file. Empty inner sets are pruned so the
        blob only carries servers with at least one disabled tool.
        Silent on filesystem errors — a save that fails should not
        crash the caller's tool-toggle path."""
        path = self.path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "disabled": {
                    server: sorted(tools) for server, tools in disabled.items() if tools
                }
            }
            path.write_text(json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            logger.warning("MCP tool state write failed (%s): %s", path, exc)
