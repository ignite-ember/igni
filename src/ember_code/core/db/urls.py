"""Pure primitive helpers for SQLite path + URL derivation.

These functions take only primitives (``str | Path``), hold no state,
and share no cross-call invariants — they qualify for the pure-helper
exception to the "OOP over free functions" rule. They live here (not
on :class:`EngineRegistry`) so both the registry, the ``engine.py``
compatibility shim, and ``migrations.py`` can import them without
circular dependencies.
"""

from __future__ import annotations

from pathlib import Path


def _normalize_path(path: str | Path) -> str:
    """Resolve ``~`` and relative paths to a canonical absolute path."""
    return str(Path(str(path)).expanduser().resolve())


def _ensure_parent(path: str | Path) -> None:
    """Create the parent directory of ``path`` if it doesn't exist."""
    Path(_normalize_path(path)).parent.mkdir(parents=True, exist_ok=True)


def sync_url(path: str | Path) -> str:
    """SQLAlchemy URL for sync access (used by alembic)."""
    return f"sqlite:///{_normalize_path(path)}"


def async_url(path: str | Path) -> str:
    """SQLAlchemy URL for async access via aiosqlite."""
    return f"sqlite+aiosqlite:///{_normalize_path(path)}"
