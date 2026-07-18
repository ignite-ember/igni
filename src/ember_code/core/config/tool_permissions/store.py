"""Thin :class:`ToolPermissions` orchestrator.

Public API unchanged from the pre-refactor procedural module — the
class still exposes :meth:`check`, :meth:`get_level`,
:meth:`is_denied`, :meth:`needs_confirmation`, :meth:`has_arg_rules`,
and :meth:`save_rule` so the eight importers stay untouched.

What changed: instead of owning everything (defaults, file I/O,
parsing, matching), this class composes four small collaborators —
:class:`ToolPermissionDefaults`, :class:`SettingsFileLoader`,
:class:`SettingsFileWriter`, and :class:`ToolNameResolver` — and
delegates each responsibility to them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember_code.core.config.settings import PermissionsConfig
from ember_code.core.config.tool_permissions.schemas import (
    CategoryToToolMap,
    PermissionLevel,
    PermissionRule,
    ToolInvocationArgs,
    ToolPermissionDefaults,
)
from ember_code.core.config.tool_permissions.settings_files import (
    SettingsFileLoader,
    SettingsFileWriter,
)
from ember_code.core.config.tool_permissions.tool_name_resolver import ToolNameResolver

logger = logging.getLogger(__name__)


class ToolPermissions:
    """Resolves per-tool permission levels from settings files.

    Supports both bare tool rules (``"Bash"``) and argument-specific
    rules (``"Bash(git status)"``, ``"WebFetch(domain:github.com)"``).

    Resolution order for a specific call:

    1. Argument-specific rules (last matching rule wins).
    2. Bare tool-level rule.
    3. Default level for the tool (``"ask"`` when unknown).
    """

    def __init__(
        self,
        project_dir: Path | None = None,
        settings_permissions: PermissionsConfig | None = None,
    ) -> None:
        self._project_dir = project_dir or Path.cwd()

        # Collaborators (composition, not inheritance)
        self._defaults = ToolPermissionDefaults()
        self._loader = SettingsFileLoader(project_dir=self._project_dir)
        self._writer = SettingsFileWriter(project_dir=project_dir)
        self._resolver = ToolNameResolver()
        self._category_map = CategoryToToolMap()

        # State: bare tool-level permissions (mutable — file overrides
        # accumulate here) + typed rule list.
        self._tool_levels: dict[str, PermissionLevel] = self._defaults.as_dict()
        self._rules: list[PermissionRule] = []

        self._load_from_disk()
        if settings_permissions is not None:
            self._apply_settings_overrides(settings_permissions)

    # ── Loading ──────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """Walk the settings-file chain via :class:`SettingsFileLoader`
        and fold each file into the in-memory state."""
        for result in self._loader.load_all():
            if not result.ok or result.file is None:
                continue
            for level, raw_rule in result.file.rules_by_level():
                self._absorb_raw_rule(raw_rule, level)

    def _absorb_raw_rule(self, raw_rule: str, level: PermissionLevel) -> None:
        """Parse ``raw_rule`` and merge into the appropriate state.

        Bare rules (no argument pattern) mutate :attr:`_tool_levels`;
        arg-specific rules append to :attr:`_rules`.
        """
        parsed = PermissionRule.parse(raw_rule, level=level)
        if parsed is None:
            logger.warning("Skipping unparseable permission rule: %r", raw_rule)
            return
        # Detect "bare" via the raw pattern rather than isinstance —
        # keeps the store class ignorant of the RuleArgPattern
        # hierarchy details.
        if not parsed.arg_pattern.raw:
            self._tool_levels[parsed.tool_name] = level
        else:
            self._rules.append(parsed)

    def _apply_settings_overrides(self, cfg: PermissionsConfig) -> None:
        """Layer ``settings.permissions`` (CLI / config YAML) on top
        of the file-derived levels.

        These take priority over anything in the settings.json files
        — same as the pre-refactor behaviour.
        """
        defaults = self._defaults
        for level, tools in self._category_map.iter_config_levels(cfg):
            if level not in ("allow", "ask", "deny"):
                logger.warning(
                    "PermissionsConfig has invalid level %r for tools %s; skipping",
                    level,
                    tools,
                )
                continue
            for tool in tools:
                if defaults.for_tool(tool) != level:
                    self._tool_levels[tool] = level

    # ── Public API ───────────────────────────────────────────────

    def check(
        self,
        tool_name: str,
        func_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
    ) -> PermissionLevel:
        """Check permission for a specific tool call.

        Args:
            tool_name: Our tool name (e.g. ``"Bash"``, ``"Write"``).
            func_name: Agno function name (e.g. ``"run_shell_command"``).
            tool_args: The actual arguments being passed.

        Returns:
            ``"allow"``, ``"ask"``, or ``"deny"``.
        """
        resolved = self._resolver.resolve(tool_name=tool_name, func_name=func_name)
        args = ToolInvocationArgs.from_dict(tool_args)

        # Last matching arg-rule wins — same precedence as pre-refactor.
        matched: PermissionLevel | None = None
        for rule in self._rules:
            if rule.matches(resolved, args):
                matched = rule.level

        if matched is not None:
            return matched

        return self._tool_levels.get(resolved, "ask")

    def get_level(self, tool_name: str) -> PermissionLevel:
        """Bare tool-level (no arg check) — used at registry time."""
        return self._tool_levels.get(tool_name, "ask")

    def is_denied(self, tool_name: str) -> bool:
        return self.get_level(tool_name) == "deny"

    def needs_confirmation(self, tool_name: str) -> bool:
        return self.get_level(tool_name) == "ask"

    def has_arg_rules(self, tool_name: str) -> bool:
        """Are there any argument-specific rules for ``tool_name``?"""
        return any(r.tool_name == tool_name for r in self._rules)

    def save_rule(self, rule: str, level: PermissionLevel) -> None:
        """Persist a permission rule to ``.ember/settings.local.json``
        (project-local, falls back to home-local).

        Delegates disk I/O to :class:`SettingsFileWriter` and then
        updates the in-memory state so subsequent :meth:`check`
        calls see the new rule without a reload.
        """
        self._writer.save_rule(rule, level)
        self._absorb_raw_rule(rule, level)
