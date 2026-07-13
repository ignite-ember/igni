"""Small JSON read/write helpers used by the init flow.

Extracted from ``init.py`` (iter 48) so both
``init_checksums.py`` and ``init.py`` share a single implementation
of the fail-soft-load + mkdir-parents-then-write pair.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_json(path: Path) -> dict:
    """Load a JSON file, returning empty dict if missing or invalid."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_json(path: Path, data: dict) -> None:
    """Write a dict as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
