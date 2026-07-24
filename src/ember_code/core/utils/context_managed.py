"""Sysadmin-enforced managed policy — ``ember.md`` / ``CLAUDE.md``
in a platform-specific write-protected directory.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8 (small modules, one responsibility).

The "managed" tier is one of six rule-loading tiers the session
prompt merges (see the top of ``context.py`` for the full list).
Managed rules come first because they represent org policy that
users can't opt out of — same rationale as the managed-settings
file next door.

## Platform-specific directories

- macOS: ``/Library/Application Support/Ember/``
- Linux: ``/etc/ember/``
- Windows: ``%PROGRAMDATA%/Ember/`` (defaults to ``C:\\ProgramData/Ember``)
- Anything else: no managed tier.

## Security note

``@<path>.md`` imports inside a managed policy resolve against the
managed directory itself — a managed policy can't reach into
``/etc/passwd`` or the user's project via ``@/...``. Enforced by
``_read_rules_dir``'s ``allowed_root`` guard.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path


def _platform_managed_rules_dir() -> Path | None:
    """OS-specific directory that may host a sysadmin-enforced
    instructions file (``ember.md`` and/or ``CLAUDE.md``).

    Sibling to the managed-settings file — both live in the same
    write-protected parent so a sysadmin / MDM profile can drop a
    full policy bundle (settings + instructions) in one place.
    Returns ``None`` on unknown platforms; the loader treats that
    as "no managed instructions tier."
    """
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Ember")
    if sys.platform.startswith("linux"):
        return Path("/etc/ember")
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "Ember"
    return None


def load_managed_rules(
    read_rules_dir: Callable[..., str],
    rules_filenames: Callable[[bool], tuple[str, ...]],
    platform_dir_fn: Callable[[], Path | None] = _platform_managed_rules_dir,
    read_claude_md: bool = True,
) -> str:
    """Load the sysadmin-enforced managed-policy instructions file.

    Reads ``ember.md`` (and ``CLAUDE.md`` when ``read_claude_md``)
    from the platform's managed directory. ``@<path>.md`` imports
    inside those files resolve against the managed directory
    itself — a managed policy can't reach into ``/etc/passwd`` or
    the user's project via ``@/...``.

    Returns ``""`` when no managed dir is defined for this
    platform, the dir doesn't exist, or no managed instructions
    files were found there.

    Mirrors Claude Code's enterprise-managed ``CLAUDE.md`` tier:
    the file is prepended to the rules block under a "Managed
    Policy" header so the model sees the org-pinned guidance
    before any user/project rules.

    All three helpers are injected — ``read_rules_dir`` and
    ``rules_filenames`` live in ``context.py``; ``platform_dir_fn``
    defaults to the local platform lookup but tests substitute it
    via the ``context`` module-level name so monkeypatching there
    still works. Keeps this module a leaf in the import graph per
    CODE_STANDARDS Rule 2.
    """
    managed_dir = platform_dir_fn()
    if managed_dir is None:
        return ""
    try:
        if not managed_dir.is_dir():
            return ""
    except OSError:
        return ""
    return read_rules_dir(
        managed_dir,
        rules_filenames(read_claude_md),
        allowed_root=managed_dir,
    )
