"""Ephemeral agent store — the ``.ember/agents.tmp`` lifecycle.

Owns:

* The on-disk ``agents.tmp`` directory (create, rehydrate on
  restart).
* Ephemeral-agent CRUD: register + list + promote to permanent +
  discard + cleanup.

Ephemeral registration writes a ``.md`` file into the tmp dir, then
delegates registration back to the pool via its public API. No
private-attribute reach-in.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.agents.schemas import (
    AgentDefinition,
    AgentEntry,
    AgentPriority,
)
from ember_code.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agno.agent import Agent

    from ember_code.core.agents.pool import AgentPool


class EphemeralAgentStore:
    """Manages agents created mid-session via ``/agents create``.

    Ephemerals live in ``<project>/.ember/agents.tmp/`` as
    ``.md`` files and get :attr:`AgentPriority.EPHEMERAL` — the
    highest priority — so they win against any base entry.
    """

    DEFAULT_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Bash", "Grep", "Glob")

    def __init__(
        self,
        project_dir: Path,
        pool: AgentPool,
        max_ephemeral: int = 5,
    ) -> None:
        self._dir: Path = project_dir / ".ember" / "agents.tmp"
        self._pool = pool
        self._max = max_ephemeral

    @property
    def directory(self) -> Path:
        """Path to the on-disk tmp directory."""
        return self._dir

    @property
    def max_ephemeral(self) -> int:
        return self._max

    @max_ephemeral.setter
    def max_ephemeral(self, value: int) -> None:
        self._max = value

    @property
    def count(self) -> int:
        """Derived — no separate counter to keep in lockstep.

        Was five setters/clearers in the old god-class (AP3).
        Now one source of truth: whatever's tagged EPHEMERAL in
        the pool.
        """
        return sum(
            1 for entry in self._pool.iter_entries() if entry.priority == AgentPriority.EPHEMERAL
        )

    # ── Lifecycle ────────────────────────────────────────────────

    def init(self) -> None:
        """Create the tmp dir and re-load any leftovers from a
        crashed previous session.

        The base pool load path deliberately SKIPS ``agents.tmp``
        so a fresh boot doesn't advertise stale ephemerals. This
        method opts back in explicitly."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pool.load_ephemeral_directory(self._dir)

    # ── CRUD ──────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[str] | None = None,
        model: str | None = None,
    ) -> Agent:
        """Create an ephemeral agent, write its ``.md``, and add
        to the pool.

        Uses :meth:`ToolRegistry.normalize_agno_names` to map the
        function-name-to-registry-name aliases and reject unknown
        tools before the file lands on disk.
        """
        if self.count >= self._max:
            raise ValueError(
                f"Ephemeral agent limit reached ({self._max}). "
                f"Promote or remove existing ephemeral agents first."
            )
        if self._pool.has_definition(name):
            raise ValueError(f"Agent '{name}' already exists in the pool.")

        resolved_tools = ToolRegistry.normalize_agno_names(list(tools or self.DEFAULT_TOOLS))

        md_content = self._render_markdown(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=resolved_tools,
            model=model,
        )
        md_path = self._dir / f"{name}.md"
        md_path.write_text(md_content)

        definition = AgentMarkdownFile(md_path).parse()
        self._pool.upsert_entry(AgentEntry(definition=definition, priority=AgentPriority.EPHEMERAL))
        return self._pool.get(name)

    def list_agents(self) -> list[AgentDefinition]:
        """Ephemeral definitions currently registered in the pool."""
        return [
            entry.definition
            for entry in self._pool.iter_entries()
            if entry.definition.source_path and self._dir in entry.definition.source_path.parents
        ]

    def promote(self, name: str, project_dir: Path) -> Path:
        """Move an ephemeral's ``.md`` to the permanent agents dir
        and re-tag it in the pool."""
        entry = self._pool.get_entry(name)
        defn = entry.definition
        if not defn.source_path or self._dir not in defn.source_path.parents:
            raise ValueError(f"Agent '{name}' is not an ephemeral agent.")

        dest_dir = project_dir / ".ember" / "agents"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / defn.source_path.name

        shutil.move(str(defn.source_path), str(dest_path))

        defn.source_path = dest_path
        self._pool.upsert_entry(AgentEntry(definition=defn, priority=AgentPriority.PROJECT_EMBER))
        return dest_path

    def discard(self, name: str) -> None:
        """Delete an ephemeral from disk and remove from the pool."""
        entry = self._pool.get_entry(name)
        defn = entry.definition
        if not defn.source_path or self._dir not in defn.source_path.parents:
            raise ValueError(f"Agent '{name}' is not an ephemeral agent.")
        if defn.source_path.exists():
            defn.source_path.unlink()
        self._pool.remove(name)

    def cleanup(self) -> int:
        """Delete every ephemeral from disk + pool. Returns count."""
        ephemeral = self.list_agents()
        for defn in ephemeral:
            if defn.source_path and defn.source_path.exists():
                defn.source_path.unlink()
            self._pool.remove(defn.name)
        return len(ephemeral)

    # ── Internals ────────────────────────────────────────────────

    def _render_markdown(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[str],
        model: str | None,
    ) -> str:
        tools_str = ", ".join(tools)
        lines = [
            "---",
            f"name: {name}",
            f"description: {description}",
            f"tools: {tools_str}",
        ]
        if model:
            lines.append(f"model: {model}")
        lines.append("---")
        lines.append(system_prompt)
        lines.append("")
        return "\n".join(lines)


__all__ = ["EphemeralAgentStore"]
