"""Permission policy — the pure decision layer.

Runs the shared evaluation pipeline (blocklist / protected-path guard
→ per-category level → allowlist → defer-to-ask) once for all three
:class:`PermissionCategory` values, collapsing the three near-identical
``check_file_read`` / ``check_file_write`` / ``check_shell_execute``
bodies from the pre-refactor module into a single ``evaluate``.

Has no I/O of its own — reads settings, delegates persistence to
:class:`AllowlistStore`. The interactive prompt lives in
:class:`ApprovalPrompt`.
"""

import fnmatch
import logging
from pathlib import Path

from ember_code.core.config.permissions.allowlist_store import AllowlistStore
from ember_code.core.config.permissions.schemas import (
    DecisionSource,
    DecisionVerdict,
    GuardDecision,
    PermissionCategory,
    PermissionLevel,
    PermissionRequest,
)
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class PermissionPolicy:
    """Pure evaluation logic — no console, no persistence writes.

    Composed of a :class:`Settings` (for policy levels, protected
    paths, blocked commands, require-confirmation prefixes) and an
    :class:`AllowlistStore` (for user-saved approvals).
    """

    def __init__(self, settings: Settings, allowlist: AllowlistStore) -> None:
        self._settings = settings
        self._allowlist = allowlist

    # ── read-only accessors ───────────────────────────────────────

    @property
    def allowlist(self) -> AllowlistStore:
        """The underlying store — exposed so ``ApprovalPrompt`` can
        write user-approved patterns back through the same instance."""
        return self._allowlist

    # ── predicate methods (were state-first free helpers before) ─

    def level_for(self, category: PermissionCategory) -> PermissionLevel:
        """Resolve the legacy per-category level string on
        :class:`PermissionsConfig` to a :class:`PermissionLevel`.

        A user misconfiguration (e.g. ``file_write: aloow`` in
        ``settings.yaml``) previously silently defaulted to ``"ask"``
        through ``getattr(..., "ask")``. We preserve that fail-safe —
        unrecognised values return :attr:`PermissionLevel.ASK` — but
        emit a ``logger.warning`` so the misconfig is visible.
        """
        raw = getattr(self._settings.permissions, category.value, "ask")
        try:
            return PermissionLevel(raw)
        except ValueError:
            logger.warning(
                "permissions.%s has unrecognised level %r; defaulting to 'ask'",
                category.value,
                raw,
            )
            return PermissionLevel.ASK

    def is_protected_path(self, path: str) -> bool:
        """True if ``path`` matches any glob in
        ``settings.safety.protected_paths`` (either by basename or by
        full-path match)."""
        for pattern in self._settings.safety.protected_paths:
            if fnmatch.fnmatch(Path(path).name, pattern):
                return True
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def is_blocked_command(self, command: str) -> bool:
        """True if ``command`` contains any substring listed under
        ``settings.safety.blocked_commands``."""
        return any(blocked in command for blocked in self._settings.safety.blocked_commands)

    def requires_confirmation(self, command: str) -> bool:
        """True if ``command`` starts with any prefix listed under
        ``settings.safety.require_confirmation`` — forces an ask even
        when ``shell_execute`` is set to ``allow``."""
        return any(
            command.startswith(prefix) for prefix in self._settings.safety.require_confirmation
        )

    # ── main pipeline ─────────────────────────────────────────────

    def evaluate(self, request: PermissionRequest) -> GuardDecision:
        """Run the shared pipeline for one request.

        Ordering: hard blockers (protected paths / blocked commands)
        → shell require-confirmation prefixes (defer to prompt even
        when ``shell_execute='allow'`` — ``git push`` still asks) →
        per-category level → allowlist → defer.
        """
        category = request.category
        value = request.value

        # Hard blockers first — protected paths and blocked commands
        # cannot be overridden by settings level or allowlist.
        if category is PermissionCategory.FILE_WRITE and self.is_protected_path(value):
            return GuardDecision(
                allowed=False,
                verdict=DecisionVerdict.DENY,
                reason=f"{value} is a protected path.",
                source=DecisionSource.PROTECTED,
            )
        if category is PermissionCategory.SHELL_EXECUTE and self.is_blocked_command(value):
            return GuardDecision(
                allowed=False,
                verdict=DecisionVerdict.DENY,
                reason="Command matches blocked pattern.",
                source=DecisionSource.BLOCKED,
            )

        # For shell, ``require_confirmation`` prefixes win over the
        # per-category level. ``shell_execute='allow'`` MUST still
        # route ``git push`` to the interactive prompt.
        if category is PermissionCategory.SHELL_EXECUTE and self.requires_confirmation(value):
            return GuardDecision(
                allowed=False,
                verdict=DecisionVerdict.DEFER,
                reason=f"{category.value} requires user confirmation.",
                source=DecisionSource.POLICY,
            )

        # Per-category level.
        level = self.level_for(category)
        if level is PermissionLevel.ALLOW:
            return GuardDecision(
                allowed=True,
                verdict=DecisionVerdict.ALLOW,
                reason=f"permissions.{category.value}=allow",
                source=DecisionSource.POLICY,
            )
        if level is PermissionLevel.DENY:
            return GuardDecision(
                allowed=False,
                verdict=DecisionVerdict.DENY,
                reason=f"permissions.{category.value}=deny",
                source=DecisionSource.POLICY,
            )

        # Level is ASK — allowlist can still short-circuit to allow.
        if self._allowlist.matches(category, value):
            return GuardDecision(
                allowed=True,
                verdict=DecisionVerdict.ALLOW,
                reason=f"matches saved allowlist for {category.value}",
                source=DecisionSource.ALLOWLIST,
            )

        # Defer to the interactive prompt.
        return GuardDecision(
            allowed=False,
            verdict=DecisionVerdict.DEFER,
            reason=f"permissions.{category.value}=ask",
            source=DecisionSource.POLICY,
        )
