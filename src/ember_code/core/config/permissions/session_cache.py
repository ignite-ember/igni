"""Session-scoped one-time approval cache.

Replaces the ``self._session_approvals: set[str]`` bag on the
pre-refactor ``PermissionGuard`` — a set of stringly-typed
``f"{category}:{value}"`` keys — with a typed ``set[tuple[
PermissionCategory, str]]``.
"""

from ember_code.core.config.permissions.schemas import PermissionCategory


class SessionApprovalCache:
    """Remembers ``(category, value)`` pairs the user approved once
    for the duration of a single session. Not persisted."""

    def __init__(self) -> None:
        self._entries: set[tuple[PermissionCategory, str]] = set()

    def remember(self, category: PermissionCategory, value: str) -> None:
        """Record a one-time approval."""
        self._entries.add((category, value))

    def contains(self, category: PermissionCategory, value: str) -> bool:
        """True if the user already approved this exact pair this
        session."""
        return (category, value) in self._entries
