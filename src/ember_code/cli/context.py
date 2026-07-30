"""Typed :class:`click.Context.obj` payload.

Replaces the raw ``dict`` bag that used to live behind
``ctx.ensure_object(dict)`` with a small typed class so any
consumer that reaches for e.g. ``ctx.obj.settings`` gets a real
attribute (and a real ``AttributeError`` if the shape drifts)
instead of a silent ``KeyError``.

A ``.get(...)`` shim is preserved for backward compat with any
downstream code that still uses ``ctx.obj.get("settings")`` — the
old dict-shaped access continues to work while new call sites can
use attribute access.

Deliberately NOT a Pydantic model: the ``Settings`` slot needs to
accept ``MagicMock`` instances during CLI unit tests (which patch
``load_settings`` and don't want to construct a real
:class:`Settings`), and Pydantic's ``arbitrary_types_allowed``
still runs an ``isinstance`` check that rejects the mock. A
plain class with type hints gives us the same "typed attribute
access" ergonomics without the runtime rejection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ember_code.core.config.settings import Settings
from ember_code.core.worktree import WorktreeManager


class CliContext:
    """Everything the CLI stashes on :attr:`click.Context.obj`."""

    __slots__ = ("settings", "worktree_manager", "project_dir", "additional_dirs")

    def __init__(
        self,
        settings: Settings,
        worktree_manager: WorktreeManager | None = None,
        project_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.worktree_manager = worktree_manager
        self.project_dir = project_dir
        self.additional_dirs = additional_dirs

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-shaped accessor for legacy call sites.

        The pre-refactor CLI stored a raw ``dict`` on ``ctx.obj``
        and readers used ``ctx.obj.get("settings")``. Preserving
        the method keeps those sites working without a coordinated
        change.
        """
        return getattr(self, key, default)
