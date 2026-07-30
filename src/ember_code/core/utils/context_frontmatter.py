"""YAML frontmatter parsing + path-glob matching for rules files.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8. This is shared infrastructure used by
every per-source loader (``context_user``, ``context_project``,
``context_managed``) — the parent module still owns the composition;
this file holds the frontmatter primitives.

## What lives here

- :func:`parse_frontmatter` — extract a ``paths:`` list from a rules
  file's YAML frontmatter. Returns ``([], body)`` when there is no
  frontmatter or no ``paths`` key, meaning the rule applies
  unconditionally.
- :func:`matches_paths` — decide whether a path-scoped rule applies
  to the current session, given the working directory + project
  directory.

Both are pure — no I/O, no globals. Testable in isolation.
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
from pathlib import Path

# Same regex the parent module used — matches ``---\n<yaml>\n---\n<body>``.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[list[str], str]:
    """Extract a ``paths`` list from YAML frontmatter; return (paths, body).

    Returns ``([], content)`` if there is no frontmatter or no
    ``paths`` key — meaning the rule applies unconditionally.

    Deliberately does NOT depend on PyYAML. The parser handles two
    forms:
    - Inline list: ``paths: ["docs/**", "tests/**"]``
    - Block list:  ``paths:\\n  - docs/**\\n  - tests/**``

    Other frontmatter keys are ignored — callers only care about
    ``paths`` here. A future extension (e.g. ``priority``) would
    add a sibling parse function, not extend this one.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return [], content
    fm, body = match.group(1), match.group(2)
    if "paths:" not in fm:
        return [], body
    paths: list[str] = []
    in_block = False
    for line in fm.splitlines():
        stripped = line.strip()
        if stripped.startswith("paths:"):
            in_block = True
            inline = stripped[len("paths:") :].strip()
            if inline.startswith("[") and inline.endswith("]"):
                for item in inline[1:-1].split(","):
                    item = item.strip().strip('"').strip("'")
                    if item:
                        paths.append(item)
                in_block = False
            continue
        if in_block:
            if stripped.startswith("- "):
                value = stripped[2:].strip().strip('"').strip("'")
                if value:
                    paths.append(value)
            elif stripped and not line.startswith((" ", "\t")):
                in_block = False
    return paths, body


def matches_paths(
    paths: list[str],
    working_dir: Path | None,
    project_dir: Path | None,
) -> bool:
    """Best-effort: decide whether a path-scoped rule applies to this session.

    - No paths → always applies (return ``True``).
    - No working directory → conservative: return ``False`` (can't
      match a glob without a candidate path to match against).
    - Otherwise: try each glob against the absolute working directory
      AND (when we can compute it) the path relative to
      ``project_dir``. Match ANY glob → applies.

    Uses ``fnmatch``, so ``**`` doesn't recurse the way glob() does —
    ``docs/**/*.md`` matches ``docs/foo.md`` and ``docs/a/b.md``
    equally, which matches the Claude Code convention for rules-file
    scoping.
    """
    if not paths:
        return True
    if working_dir is None:
        return False
    working = working_dir.resolve()
    candidates = [str(working)]
    if project_dir is not None:
        with contextlib.suppress(ValueError):
            candidates.append(str(working.relative_to(project_dir.resolve())))
    for pattern in paths:
        for cand in candidates:
            if fnmatch.fnmatch(cand, pattern):
                return True
    return False
