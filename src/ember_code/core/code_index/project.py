"""Project identifier derivation for ember-code.

Each project gets its own data directory + Chroma file + SQLite file,
keyed by a stable hash derived from the git remote URL when available
(stable across clones of the same repo) and the absolute project
directory when not.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_project_id(project_dir: str | Path) -> str:
    """Return a stable project id for ``project_dir``.

    Uses the git remote origin URL when available; falls back to the
    absolute path. Both are SHA-256 hashed and truncated — long enough
    to make collisions astronomically unlikely across one user's
    projects.
    """
    project_id = str(Path(project_dir))
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            project_id = result.stdout.strip()
    except Exception as exc:
        logger.debug("git remote lookup failed for %s: %s", project_dir, exc)

    return hashlib.sha256(project_id.encode()).hexdigest()[:16]
