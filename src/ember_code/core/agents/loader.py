"""Filesystem discovery + priority resolution for agent ``.md`` files.

Owns three concerns:

* Scanning the five standard roots (user/project × Claude/Ember +
  the project-local variant) and applying the priority table so
  the higher-priority definition wins on same-name collisions.
* Picking the right CodeIndex prompt variant (``<name>.md`` vs
  ``<name>.codeindex.md``) based on whether the current HEAD has
  a populated chroma.
* Applying the plugin security envelope when loading from a
  plugin-shipped ``agents/`` directory.

Emits a typed :class:`LoadReport` instead of printing to stderr —
callers surface the errors through their own channel (audit log,
FE broadcast, etc.).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy
from ember_code.core.agents.schemas import (
    AgentEntry,
    AgentPriority,
    LoadError,
    LoadReport,
)

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class AgentDefinitionLoader:
    """Scan the standard agent roots and return a merged
    :class:`LoadReport`.

    ``codeindex_available`` is typed instance state — not a
    ``getattr(self, "_codeindex_available", False)`` reach-in.
    ``restriction_policy`` is optional: passing one flips the
    plugin security envelope on for every loaded agent, so the
    same class serves both the base-load path and the plugin-load
    path.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path,
        codeindex_available: bool,
        restriction_policy: PluginRestrictionPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._project_dir = project_dir
        self._codeindex_available = codeindex_available
        self._policy = restriction_policy

    def load(self) -> LoadReport:
        """Scan the five standard roots and return an aggregated
        :class:`LoadReport`.

        Roots are hit in priority order; higher-priority entries
        upsert lower-priority ones with the same name."""
        settings = self._settings
        project_dir = self._project_dir

        dirs: list[tuple[Path, AgentPriority]] = [
            (Path.home() / ".ember" / "agents", AgentPriority.USER_EMBER),
            (project_dir / ".ember" / "agents.local", AgentPriority.PROJECT_LOCAL),
            (project_dir / ".ember" / "agents", AgentPriority.PROJECT_EMBER),
        ]

        if settings.agents.cross_tool_support:
            dirs.append((project_dir / ".claude" / "agents", AgentPriority.PROJECT_CLAUDE))
            dirs.append((Path.home() / ".claude" / "agents", AgentPriority.USER_CLAUDE))

        report = LoadReport()
        for directory, priority in dirs:
            partial = self.load_directory(directory, priority)
            report.merge(partial)
        return report

    def load_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        namespace: str | None = None,
    ) -> LoadReport:
        """Parse ``.md`` files from ``path`` into a :class:`LoadReport`.

        Skips the wrong CodeIndex variant per
        ``self._codeindex_available``. ``namespace`` prefixes
        every loaded agent's ``name`` as ``<namespace>:<name>`` —
        used by the plugin loader.
        """
        report = LoadReport()
        if not path.exists():
            return report

        typed_priority: AgentPriority = (
            priority if isinstance(priority, AgentPriority) else AgentPriority(int(priority))
        )

        all_files = sorted(path.glob("*.md"))
        picked = self._pick_variants(all_files)

        for md_file in picked:
            try:
                md = AgentMarkdownFile(md_file)
                definition = md.parse()
                if self._policy is not None:
                    raw_keys = md.raw_frontmatter_keys()
                    definition = self._policy.apply(
                        definition, raw_keys, plugin_name=namespace or ""
                    )
                if namespace:
                    definition = definition.namespaced(namespace)
                report.entries[definition.name] = AgentEntry(
                    definition=definition, priority=typed_priority
                )
            except Exception as exc:
                logger.warning("Failed to parse agent from %s: %s", md_file, exc)
                report.errors.append(LoadError(path=md_file, reason=str(exc)))
        return report

    def _pick_variants(self, files: list[Path]) -> list[Path]:
        """Filter out the wrong CodeIndex variant per
        ``self._codeindex_available``.

        * If CodeIndex is available, skip any plain ``*.md``
          whose sibling ``*.codeindex.md`` is also present in
          the same directory.
        * If CodeIndex is unavailable, skip every
          ``*.codeindex.md`` file (loading it would tell the
          agent to call a tool it doesn't have).
        """
        use_codeindex = self._codeindex_available
        codeindex_stems = {
            f.name[: -len(".codeindex.md")] for f in files if f.name.endswith(".codeindex.md")
        }
        picked: list[Path] = []
        for md_file in files:
            is_codeindex_variant = md_file.name.endswith(".codeindex.md")
            if is_codeindex_variant and not use_codeindex:
                continue
            if not is_codeindex_variant and use_codeindex and md_file.stem in codeindex_stems:
                continue
            picked.append(md_file)
        return picked


__all__ = ["AgentDefinitionLoader"]
