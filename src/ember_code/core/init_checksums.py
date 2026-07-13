"""Checksum-based sync for bundled agents / skills.

Extracted from ``init.py`` (iter 48) — the checksum machinery
(load, save, per-file merge decisions) is its own responsibility
and reads/writes ``.ember/.checksums.json``. Keeps the
``initialize_project`` orchestrator focused on high-level flow.

The three-way merge lives in :func:`_sync_file`:

- dst missing → copy, record hash
- dst present, no stored hash → record hash, skip (legacy)
- pkg unchanged → skip
- pkg changed, dst matches stored hash → overwrite, update hash
- pkg changed, dst diverged → write ``.new`` sidecar, warn
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ember_code.core.init_json_io import load_json, save_json

CHECKSUMS_FILE = ".checksums.json"


def file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file's content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_checksums(project_dir: Path) -> dict[str, str]:
    """Load .ember/.checksums.json — maps relative paths to original hashes."""
    path = project_dir / ".ember" / CHECKSUMS_FILE
    return load_json(path)


def save_checksums(project_dir: Path, checksums: dict[str, str]) -> None:
    """Save .ember/.checksums.json."""
    path = project_dir / ".ember" / CHECKSUMS_FILE
    save_json(path, checksums)


def sync_file(src: Path, dst: Path, key: str, checksums: dict[str, str]) -> str | None:
    """Sync a single built-in file. Returns a warning string or None.

    Logic:
      - dst doesn't exist → copy, record checksum
      - no stored checksum (legacy) → record current package hash, skip update
      - package unchanged → skip
      - package changed + user didn't modify → overwrite, update checksum
      - package changed + user modified → skip, return warning
    """
    pkg_hash = file_hash(src)
    stored_hash = checksums.get(key)

    if not dst.exists():
        # New file — copy and record
        shutil.copy2(src, dst)
        checksums[key] = pkg_hash
        return None

    if stored_hash is None:
        # Legacy: file exists but no checksum recorded.
        # Record current package hash so future updates work.
        checksums[key] = pkg_hash
        return None

    if pkg_hash == stored_hash:
        # Package hasn't changed — nothing to do
        return None

    # Package has changed — check if user modified their copy
    local_hash = file_hash(dst)

    if local_hash == stored_hash:
        # User hasn't touched it — safe to overwrite
        shutil.copy2(src, dst)
        checksums[key] = pkg_hash
        return None

    # User modified AND package updated — write new version alongside
    new_path = dst.with_suffix(dst.suffix + ".new")
    shutil.copy2(src, new_path)
    checksums[key] = pkg_hash
    return (
        f"Built-in {key} was updated but you have local modifications. "
        f"New version saved as .ember/{key}.new — diff and merge at your convenience."
    )
