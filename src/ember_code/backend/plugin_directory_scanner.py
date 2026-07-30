"""Filesystem walker that builds a :class:`PluginContents` inventory.

Owns the per-plugin-directory scan behavior formerly living in the
free function ``server_helpers._scan_plugin_dir``. Each per-subdir
step (skills / agents / hooks / MCP servers / tools / README) is a
private method so the six loops that used to be jammed into one
100-line function become independently testable.

Called only via :meth:`PluginContents.from_directory` (the classmethod
that owns model construction). To avoid an import cycle
(``plugin_schemas`` imports the scanner, scanner imports the models),
:meth:`PluginContents.from_directory` performs a lazy import of this
module inside its body.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from ember_code.backend.plugin_schemas import (
    PluginAgentInfo,
    PluginContents,
    PluginHookInfo,
    PluginMCPServerInfo,
    PluginSkillInfo,
    PluginToolInfo,
)


class PluginDirectoryScanner:
    """Walk one plugin directory and accumulate its bundled contents.

    Each ``_scan_*`` method mutates :attr:`_result` in place — the
    scanner owns the accumulator instead of threading it through
    every helper as an argument.

    :param root: The plugin directory to scan (either an installed
        plugin root or a temp-clone root during marketplace preview).
    :param name: Display name to embed in :attr:`PluginContents.name`.
    :param readme_cap: Byte cap on README excerpt to prevent a runaway
        file blowing up the wire — plugin READMEs in the wild top out
        well under the default 200_000.
    """

    def __init__(self, root: Path, name: str, readme_cap: int = 200_000) -> None:
        self._root = root
        self._name = name
        self._readme_cap = readme_cap
        self._result = PluginContents(name=name, root_path=str(root))

    def scan(self) -> PluginContents:
        """Run every per-subdir scan step and return the built model."""
        self._scan_skills()
        self._scan_agents()
        self._scan_hooks()
        self._scan_mcp_servers()
        self._scan_tools()
        self._load_readme()
        return self._result

    # ── Per-subdir scan steps ──────────────────────────────────────

    def _scan_skills(self) -> None:
        """Enumerate ``skills/*/SKILL.md`` — descriptions pulled from
        the SKILL.md frontmatter."""
        skills_dir = self._root / "skills"
        if not skills_dir.is_dir():
            return
        for sd in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = sd / "SKILL.md"
            desc = ""
            if skill_md.is_file():
                with contextlib.suppress(OSError):
                    desc = self._frontmatter_field(
                        skill_md.read_text(errors="replace"), "description"
                    )
            self._result.skills.append(PluginSkillInfo(name=sd.name, description=desc))

    def _scan_agents(self) -> None:
        """Enumerate ``agents/*.md`` — descriptions pulled from
        agent-file frontmatter."""
        agents_dir = self._root / "agents"
        if not agents_dir.is_dir():
            return
        for af in sorted(agents_dir.glob("*.md")):
            desc = ""
            with contextlib.suppress(OSError):
                desc = self._frontmatter_field(af.read_text(errors="replace"), "description")
            self._result.agents.append(PluginAgentInfo(name=af.stem, description=desc))

    def _scan_hooks(self) -> None:
        """Read ``hooks/hooks.json`` and record one entry per event
        with the handler count."""
        hooks_json = self._root / "hooks" / "hooks.json"
        if not hooks_json.is_file():
            return
        try:
            data = json.loads(hooks_json.read_text())
            for event, handlers in (data.get("hooks") or {}).items():
                if isinstance(handlers, list):
                    self._result.hooks.append(PluginHookInfo(event=event, count=len(handlers)))
        except (OSError, json.JSONDecodeError):
            pass

    def _scan_mcp_servers(self) -> None:
        """Read ``.mcp.json`` (or ``mcp.json``) and record one entry
        per server. Stops at the first file found."""
        for mcp_name in (".mcp.json", "mcp.json"):
            mcp_path = self._root / mcp_name
            if not mcp_path.is_file():
                continue
            try:
                data = json.loads(mcp_path.read_text())
                for srv_name, cfg in (data.get("mcpServers") or {}).items():
                    self._result.mcp_servers.append(
                        PluginMCPServerInfo(
                            name=srv_name,
                            transport=cfg.get("type", "stdio"),
                            command=cfg.get("command") or cfg.get("url") or "",
                        )
                    )
            except (OSError, json.JSONDecodeError):
                pass
            break

    def _scan_tools(self) -> None:
        """Enumerate ``tools/*.py`` (dunder / underscored modules
        skipped as internal)."""
        tools_dir = self._root / "tools"
        if not tools_dir.is_dir():
            return
        for tf in sorted(tools_dir.glob("*.py")):
            if tf.name.startswith("_"):
                continue
            self._result.tools.append(PluginToolInfo(name=tf.stem))

    def _load_readme(self) -> None:
        """Load the first README variant found. Truncated at
        :attr:`_readme_cap` with a marker so the wire never carries a
        pathologically large blob."""
        for readme_name in ("README.md", "Readme.md", "readme.md"):
            rp = self._root / readme_name
            if not rp.is_file():
                continue
            try:
                text = rp.read_text(errors="replace")
                if len(text) > self._readme_cap:
                    self._result.readme = (
                        text[: self._readme_cap]
                        + "\n\n_…README truncated — open the source repo for the rest._"
                    )
                else:
                    self._result.readme = text
            except OSError:
                pass
            break

    # ── Shared parsing helper ──────────────────────────────────────

    @staticmethod
    def _frontmatter_field(md_text: str, field: str) -> str:
        """Extract one key's value from a leading ``---``-delimited
        YAML frontmatter block.

        Returns ``""`` when the block is absent, unterminated, or
        missing the requested key. Values are stripped of surrounding
        whitespace and matching double quotes.
        """
        if not md_text.startswith("---"):
            return ""
        end = md_text.find("\n---", 4)
        if end <= 0:
            return ""
        for line in md_text[4:end].splitlines():
            if line.lower().startswith(f"{field}:"):
                return line.split(":", 1)[1].strip().strip('"')
        return ""
