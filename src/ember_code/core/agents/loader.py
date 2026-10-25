"""Filesystem discovery + priority resolution for agent ``.md`` files.

Owns three concerns:

* Scanning the five standard roots (user/project × Claude/Ember +
  the project-local variant) and applying the priority table so
  the higher-priority definition wins on same-name collisions.
* Loading the agents extensions stand in for: an extension's
  ``contributes.prompt.agents`` names one of igni's agents and the file
  that defines it instead, while the extension is running (and while
  its condition holds, when it names one).
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
from ember_code.core.prompts.contributions import PromptContributions, prompt_contributions

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class AgentDefinitionLoader:
    """Scan the standard agent roots and return a merged
    :class:`LoadReport`.

    ``restriction_policy`` is optional: passing one flips the
    plugin security envelope on for every loaded agent, so the
    same class serves both the base-load path and the plugin-load
    path.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path,
        restriction_policy: PluginRestrictionPolicy | None = None,
        group_dir: Path | None = None,
        contributions: PromptContributions | None = None,
    ) -> None:
        """
        Args:
            contributions: Where the agents extensions stand in for
                are registered. The process-wide table by default.
        """
        self._settings = settings
        self._project_dir = project_dir
        self._policy = restriction_policy
        self._group_dir = group_dir
        self._contributions = contributions if contributions is not None else prompt_contributions

    def load(self) -> LoadReport:
        """Scan the five standard roots and return an aggregated
        :class:`LoadReport`.

        Roots are hit in priority order; higher-priority entries
        upsert lower-priority ones with the same name.

        A group's agents are a root of their own, read straight from the
        policy cache and ranked above the user's globals but below
        anything the project declares. So an org sets the baseline and a
        repository can override one agent by name without being given a
        copy of the whole set.

        They used to be copied into ``<project>/.igni/agents`` instead,
        which is what made "the local one wins" true — there was only
        one file. Reading the cache directly gets the same outcome from
        ordering, and leaves nothing of the server's in the repository.
        """
        settings = self._settings
        project_dir = self._project_dir

        dirs: list[tuple[Path, AgentPriority]] = [
            (Path.home() / CONFIG_DIR / "agents", AgentPriority.USER_EMBER),
        ]
        if self._group_dir is not None:
            dirs.append((self._group_dir, AgentPriority.ORG_GROUP))
        dirs += [
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
        report.merge(self.load_replacements())
        return report

    def load_replacements(self) -> LoadReport:
        """The agents extensions stand in for, under igni's names.

        At :attr:`AgentPriority.EXTENSION_OVERRIDE`, above every file a
        person or an organisation wrote, so the agent behaves the way
        the running extension needs it to — and the moment the
        extension stops, or its condition stops holding, the
        replacement is gone from the table and the agent is whatever
        it was before. The file is the extension's, so it gets the
        plugin security envelope a plugin's own agents get.
        """
        report = LoadReport()
        policy = PluginRestrictionPolicy.strict()
        for name, replacement in self._contributions.agent_replacements().items():
            try:
                md = AgentMarkdownFile(replacement.path)
                definition = policy.apply(
                    md.parse(), md.raw_frontmatter_keys(), plugin_name=replacement.owner
                ).model_copy(update={"name": name})
            except Exception as exc:
                logger.warning(
                    "%s stands in for agent %s with %s, which did not load: %s",
                    replacement.owner,
                    name,
                    replacement.path,
                    exc,
                )
                report.errors.append(LoadError(path=replacement.path, reason=str(exc)))
                continue
            report.entries[name] = AgentEntry(
                definition=definition, priority=AgentPriority.EXTENSION_OVERRIDE
            )
        return report

    def load_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        namespace: str | None = None,
    ) -> LoadReport:
        """Parse ``.md`` files from ``path`` into a :class:`LoadReport`.

        ``namespace`` prefixes every loaded agent's ``name`` as
        ``<namespace>:<name>`` — used by the plugin loader, so a
        plugin's agents never take an igni agent's name. Standing in
        for one of igni's agents is :meth:`load_replacements`.
        """
        report = LoadReport()
        if not path.exists():
            return report

        typed_priority: AgentPriority = (
            priority if isinstance(priority, AgentPriority) else AgentPriority(int(priority))
        )

        for md_file in sorted(path.glob("*.md")):
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


__all__ = ["AgentDefinitionLoader"]
