"""Filesystem discovery + priority resolution for agent ``.md`` files.

Owns three concerns:

* Scanning the five standard roots (user/project × Claude/Ember +
  the project-local variant) and applying the priority table so
  the higher-priority definition wins on same-name collisions.
* Picking the right variant of an agent (``<name>.md`` vs
  ``<name>.<variant>.md``) by whether the extension that declared the
  variant is running — including an extension's own plugin replacing
  an agent it does not ship the plain version of.
* Applying the plugin security envelope when loading from a
  plugin-shipped ``agents/`` directory.

Emits a typed :class:`LoadReport` instead of printing to stderr —
callers surface the errors through their own channel (audit log,
FE broadcast, etc.).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
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
from ember_code.core.agents.variants import variants as variant_registry
from ember_code.core.paths import CONFIG_DIR

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
        known_variants: Collection[str] | None = None,
        active_variants: Collection[str] | None = None,
    ) -> None:
        """
        Args:
            known_variants: Every variant name that could appear as a
                ``<agent>.<variant>.md`` sibling, live or not. Needed
                separately from the live set so that a sibling naming
                a variant that is *installed but down* is recognised
                and skipped, rather than loaded as an agent whose name
                happens to contain a dot.
            active_variants: The subset that is live now. An extension
                reporting ``READY`` puts its name here and the agents
                written for it take over.
        """
        self._settings = settings
        self._project_dir = project_dir
        self._policy = restriction_policy
        self._group_dir = group_dir
        # The registry is what every extension with variants declares
        # into. Nothing is a variant by name alone: core knows no
        # extension, so it knows no variant until one is installed.
        self._known_variants = frozenset(
            known_variants if known_variants is not None else variant_registry.known()
        )
        self._active_variants = frozenset(
            active_variants if active_variants is not None else variant_registry.active()
        )

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
        return report

    def load_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        namespace: str | None = None,
    ) -> LoadReport:
        """Parse ``.md`` files from ``path`` into a :class:`LoadReport`.

        Keeps one file per agent (see :meth:`_pick_variants`).
        ``namespace`` prefixes every loaded agent's ``name`` as
        ``<namespace>:<name>`` — used by the plugin loader — with one
        exception, which is how an extension changes an agent it does
        not own: a plugin's ``<agent>.<variant>.md``, while its variant
        is live, *is* ``<agent>``. It loads under the plain name at
        :attr:`AgentPriority.EXTENSION_OVERRIDE`, above every file a
        person or an organisation wrote, so the agent behaves the way
        the extension that is running needs it to — and the moment the
        extension stops, the file is skipped and the agent is whatever
        it was before.
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
                entry_priority = typed_priority
                if namespace and self._variant_of(md_file):
                    entry_priority = AgentPriority.EXTENSION_OVERRIDE
                elif namespace:
                    definition = definition.namespaced(namespace)
                report.entries[definition.name] = AgentEntry(
                    definition=definition, priority=entry_priority
                )
            except Exception as exc:
                logger.warning("Failed to parse agent from %s: %s", md_file, exc)
                report.errors.append(LoadError(path=md_file, reason=str(exc)))
        return report

    def _pick_variants(self, files: list[Path]) -> list[Path]:
        """Keep one file per agent: the live variant, or the plain one.

        Convention: an agent written for a capability ships as a pair
        — ``<name>.md``, which assumes nothing, and a sibling
        ``<name>.<variant>.md`` for when that capability is live.

        * A variant file whose variant is not live is skipped.
        * The plain file is skipped when a live variant of it exists.
        * An agent with only one of the pair is loaded whichever it is:
          a half-written pair should behave like what is there.

        "Is the capability this agent was written for available?" has
        as many answers as there are extensions, and the base cannot
        know their names, so both sets come from the registry.

        Two live variants of one agent is a conflict its author made:
        both describe the same agent and only one can load. The choice
        is alphabetical so an install behaves the same way twice, and
        logged so it is findable.
        """
        live_by_stem: dict[str, list[Path]] = {}
        for md_file in files:
            variant = self._variant_of(md_file)
            if variant and variant in self._active_variants:
                live_by_stem.setdefault(self._stem_without(md_file, variant), []).append(md_file)

        for stem, candidates in live_by_stem.items():
            if len(candidates) > 1:
                chosen = min(candidates, key=lambda f: f.name)
                logger.warning(
                    "agent %s has %d live variants (%s); loading %s",
                    stem,
                    len(candidates),
                    ", ".join(sorted(f.name for f in candidates)),
                    chosen.name,
                )
                live_by_stem[stem] = [chosen]

        live = {f for group in live_by_stem.values() for f in group}

        picked: list[Path] = []
        for md_file in files:
            variant = self._variant_of(md_file)
            if variant:
                # A variant file: in only if it is the live one.
                if md_file in live:
                    picked.append(md_file)
                continue
            # A plain file: out if a live variant of it was picked.
            if md_file.stem in live_by_stem:
                continue
            picked.append(md_file)
        return picked

    @staticmethod
    def _stem_without(md_file: Path, variant: str) -> str:
        """``foo`` for ``foo.<variant>.md`` — the plain sibling's name."""
        return md_file.name[: -len(f".{variant}.md")]

    def _variant_of(self, md_file: Path) -> str:
        """The variant this file is written for, or ``""`` for none.

        Only names in ``known_variants`` count. ``my.agent.md`` is a
        plain agent called ``my.agent`` until something named ``agent``
        is installed — which is the ambiguity in the convention, and
        the reason variant names should be namespaced.
        """
        for variant in self._known_variants:
            if md_file.name.endswith(f".{variant}.md"):
                return variant
        return ""


__all__ = ["AgentDefinitionLoader"]
