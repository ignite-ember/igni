"""Pydantic models for the plugin system.

The manifest schema mirrors Claude Code's ``.claude-plugin/plugin.json``
so plugins published for Claude work here without changes. Unknown
fields are preserved (``extra="allow"``) so future Claude additions
don't break loading — they just go unused until we adopt them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

PluginRoot = Literal[
    "user-claude",  # ~/.claude/plugins/
    "user-ember",  # ~/.ember/plugins/
    "project-claude",  # <project>/.claude/plugins/
    "project-ember",  # <project>/.ember/plugins/
]


class PluginManifest(BaseModel):
    """The ``.claude-plugin/plugin.json`` schema.

    Only ``name`` is required — version/description/author are
    metadata for the plugins panel. Extra fields are preserved so
    Claude Code's manifest evolution doesn't break loading.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    description: str | None = None
    author: str | dict | None = None


class PluginSource(BaseModel):
    """Where a plugin was discovered.

    ``priority`` follows the four-root convention: project beats user,
    ember beats claude. Higher wins on same-name collisions across roots.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: PluginRoot
    path: Path
    priority: int


class PluginDefinition(BaseModel):
    """A discovered plugin: manifest + source + bundled-contents inventory.

    The ``has_*`` flags are set during scanning so the plugins panel
    can render counts without re-statting the filesystem, and so apply
    steps can skip plugins that bundle nothing in a given category.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: PluginManifest
    source: PluginSource
    has_skills: bool = False
    has_agents: bool = False
    has_hooks: bool = False
    has_mcp: bool = False
    has_tools: bool = False

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def root_path(self) -> Path:
        return self.source.path


# ── Wire-format models for the panel ───────────────────────────────
#
# The panel receives plugin/marketplace data over RPC as a list of
# dicts. These models are the contract for that shape — used to
# construct the response on the backend and reconstruct the typed
# view on the frontend. Defining them in this shared module (rather
# than the widget) keeps the source of truth in one place: if a
# field is added on the backend, the widget side picks it up
# automatically (same model).
#
# ``source_root`` is widened to ``str`` here (vs. ``PluginRoot``
# Literal on :class:`PluginSource`) since literal narrowing
# doesn't survive the JSON round-trip and these models are
# display-only.


class PluginInfo(BaseModel):
    """Wire format for one installed plugin — emitted by
    :meth:`BackendServer.get_plugin_details`, consumed by the
    plugins panel."""

    name: str
    version: str = ""
    description: str = ""
    source_root: str = ""
    path: str = ""
    enabled: bool = True
    has_skills: bool = False
    has_agents: bool = False
    has_hooks: bool = False
    has_mcp: bool = False
    has_tools: bool = False
    pin: str = ""


class MarketplacePluginInfo(BaseModel):
    """Wire format for one plugin entry inside a marketplace catalog."""

    name: str
    source: str
    description: str = ""
    version: str = ""
    branch: str = ""


class MarketplaceInfo(BaseModel):
    """Wire format for one registered marketplace — registry-level
    metadata plus the cached catalog at the time of the last fetch."""

    name: str
    url: str
    last_fetched: str = ""
    plugins: list[MarketplacePluginInfo] = []
