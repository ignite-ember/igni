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
from ember_code.core.paths import CONFIG_DIR

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
        upsert lower-priority ones with the same name.

        A group's agents are not a root of their own: they are synced
        into ``<project>/.ember/agents`` by
        :class:`~ember_code.core.init.group_agent_sync.GroupAgentSync`
        so a person can edit one. Loading the server's pristine copy at a
        higher priority as well would make that edit pointless — the
        server version would win every time.

        That sync is also what makes a group's list complete: it removes
        what the group no longer ships, and the bundle stands down while
        a group supplies agents. So the group's set simply *is* the set,
        and the roots below are the person's own additions to it.
        """
        settings = self._settings
        project_dir = self._project_dir

        dirs: list[tuple[Path, AgentPriority]] = [
            (Path.home() / CONFIG_DIR / "agents", AgentPriority.USER_EMBER),
            (project_dir / CONFIG_DIR / "agents.local", AgentPriority.PROJECT_LOCAL),
            (project_dir / CONFIG_DIR / "agents", AgentPriority.PROJECT_EMBER),
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

        Convention: every CodeIndex-aware agent ships as a
        variant pair — ``<name>.md`` (plain, no-CodeIndex) +
        sibling ``<name>.codeindex.md`` (with CodeIndex). The
        loader picks the right one based on
        ``_codeindex_available``:

        * If CodeIndex is available, prefer the ``.codeindex.md``
          variant. The plain ``<name>.md`` is skipped when the
          sibling exists.
        * If CodeIndex is unavailable, prefer the plain
          ``<name>.md`` and skip the ``.codeindex.md`` variant.

        If only one of the pair exists (older draft state), it
        is always loaded — whichever variant is present, with
        the agent's own no-CodeIndex fallback as a follow-up.
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
