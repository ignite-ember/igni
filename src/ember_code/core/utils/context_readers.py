"""Shared file-read helpers for the context rules-loading pipeline.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8. Every per-source loader
(:mod:`context_user`, :mod:`context_project`,
:mod:`context_managed`) needs these primitives — they're the "wire
level" between the pure text primitives
(:mod:`context_frontmatter`, :mod:`context_imports`) and the
per-source composition logic.

## What lives here

- :func:`read_if_exists` — read a file, swallow the "missing"
  case, log everything else. The tolerant read used everywhere.
- :func:`read_with_imports` — read + inline ``@<path>.md``
  references. Composed of `read_if_exists` + `resolve_imports`
  (from :mod:`context_imports`).
- :func:`rules_filenames` — canonical ordered tuple of rules
  filenames to check per directory, honoring the ``.local.md``
  override convention.
- :func:`read_rules_dir` — read all rules files from ONE directory,
  concatenate.
- :func:`read_rules_dir_files` — walk a ``rules/`` directory
  containing many ``*.md`` topic files, filter by frontmatter
  ``paths:`` globs, concatenate.

All I/O is defensive: missing paths and read errors return ``""``,
never raise. The rules-loading pipeline is expected to work when a
user hasn't set up a rules dir yet.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.utils.context_frontmatter import (
    matches_paths,
    parse_frontmatter,
)
from ember_code.core.utils.context_imports import resolve_imports

logger = logging.getLogger(__name__)


def read_if_exists(path: Path) -> str:
    """Read file contents if it exists, else return empty string.

    Log-and-return on any error so a stray permission-denied on a
    user's rules directory doesn't break session boot. Nothing
    upstream distinguishes "file missing" from "file unreadable" —
    both mean "no rules here."
    """
    try:
        if path.is_file():
            return path.read_text()
    except Exception as e:
        logger.debug("Failed to read rules from %s: %s", path, e)
    return ""


def read_with_imports(path: Path, allowed_root: Path) -> str:
    """Read a rules file and inline any ``@<path>.md`` references.

    Combines :func:`read_if_exists` with
    :func:`context_imports.resolve_imports`. Kept here as the single
    entry point that mixes file I/O with the pure resolver.
    """
    content = read_if_exists(path)
    if not content:
        return ""
    return resolve_imports(content, path, allowed_root)


def rules_filenames(read_claude_md: bool = True) -> tuple[str, ...]:
    """Return rules filenames to check, in load order.

    ``ember.local.md`` / ``CLAUDE.local.md`` are personal-override
    siblings of the committed files (the convention: gitignore the
    ``.local.md`` variants). They load AFTER the committed file at
    each level so their content takes precedence in any subsequent
    string concatenation that the model reads top-to-bottom.
    """
    if read_claude_md:
        return ("ember.md", "ember.local.md", "CLAUDE.md", "CLAUDE.local.md")
    return ("ember.md", "ember.local.md")


def read_rules_dir(
    directory: Path,
    filenames: tuple[str, ...] = ("ember.md", "CLAUDE.md"),
    allowed_root: Path | None = None,
) -> str:
    """Read rules from a directory, checking all candidate filenames.

    Returns concatenated contents of all found files. When
    ``allowed_root`` is provided, ``@<path>.md`` imports resolve
    inside it (otherwise the file's own directory is the root,
    which is fine for the user-rules dir-form callers).
    """
    root = allowed_root if allowed_root is not None else directory
    parts: list[str] = []
    for name in filenames:
        content = read_with_imports(directory / name, allowed_root=root)
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def read_rules_dir_files(
    directory: Path,
    working_dir: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Concatenate all ``*.md`` files in a rules directory, respecting ``paths:``.

    ``@<path>.md`` imports inside each file resolve against
    ``directory`` — keeps user-level rules from reaching into the
    project (and vice versa).

    Files carrying a ``paths:`` YAML frontmatter contribute only
    when the session's ``working_dir`` matches one of the globs.
    Files without frontmatter (or without a ``paths:`` key) always
    contribute — see :func:`context_frontmatter.matches_paths` for
    the semantics.
    """
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        content = read_with_imports(path, allowed_root=directory)
        if not content:
            continue
        paths_filter, body = parse_frontmatter(content)
        if not matches_paths(paths_filter, working_dir, project_dir):
            continue
        body = body.strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts)
