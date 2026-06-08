"""Context utilities — hierarchical project rules loading.

Loads rules from several sources, all merged into the session prompt:

1. **User-level**
   - ``~/.ember/rules.md`` — legacy single-file form
   - ``~/.ember/rules/*.md`` — directory form (one file per topic)
   - ``~/.claude/rules/*.md`` — cross-tool form, gated on ``cross_tool_support``
2. **Project root** — ``ember.md`` / ``CLAUDE.md`` at the project root
3. **Subdirectory** — ``ember.md`` / ``CLAUDE.md`` in any parent of the
   working file, walking up to the project root

Files in ``rules/`` directories may carry YAML frontmatter with a
``paths`` list of globs. ember-code loads at session start (not on file
read like Claude Code), so those rules contribute only when the
session's working directory matches one of the listed globs — coarser
than Claude Code's read-time matching, but enough to keep scoped rules
out of unrelated sessions.
"""

import contextlib
import fnmatch
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

USER_RULES_PATH = Path.home() / ".ember" / "rules.md"
USER_RULES_DIR = Path.home() / ".ember" / "rules"
CLAUDE_USER_RULES_DIR = Path.home() / ".claude" / "rules"


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[list[str], str]:
    """Extract a ``paths`` list from YAML frontmatter; return (paths, body).

    Returns ``([], content)`` if there is no frontmatter or no ``paths``
    key — meaning the rule applies unconditionally.
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


def _matches_paths(
    paths: list[str], working_dir: Path | None, project_dir: Path | None
) -> bool:
    """Best-effort: decide whether a path-scoped rule applies to this session.

    No paths → always applies. Otherwise the rule contributes only when
    the session's working directory matches one of the globs (absolute
    path, or path relative to ``project_dir`` if available).
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


def _read_if_exists(path: Path) -> str:
    """Read file contents if it exists, else return empty string."""
    try:
        if path.is_file():
            return path.read_text()
    except Exception as e:
        logger.debug("Failed to read rules from %s: %s", path, e)
    return ""


def _read_rules_dir_files(
    directory: Path,
    working_dir: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Concatenate all ``*.md`` files in a rules directory, respecting ``paths:``."""
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except Exception as e:
            logger.debug("Failed to read rules file %s: %s", path, e)
            continue
        paths_filter, body = _parse_frontmatter(content)
        if not _matches_paths(paths_filter, working_dir, project_dir):
            continue
        body = body.strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def _rules_filenames(read_claude_md: bool = True) -> tuple[str, ...]:
    """Return the list of rules filenames to check."""
    if read_claude_md:
        return ("ember.md", "CLAUDE.md")
    return ("ember.md",)


def _read_rules_dir(directory: Path, filenames: tuple[str, ...] = ("ember.md", "CLAUDE.md")) -> str:
    """Read rules from a directory, checking all candidate filenames.

    Returns concatenated contents of all found files, separated by newlines.
    """
    parts: list[str] = []
    for name in filenames:
        content = _read_if_exists(directory / name)
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def load_user_rules(
    working_dir: Path | None = None,
    project_dir: Path | None = None,
    read_claude_rules: bool = True,
) -> str:
    """Load user-level global rules from all configured sources.

    Sources, concatenated in order:
    1. ``~/.ember/rules.md`` (legacy single-file form)
    2. ``~/.ember/rules/*.md`` (directory form)
    3. ``~/.claude/rules/*.md`` (cross-tool, when ``read_claude_rules``)

    Files with ``paths:`` frontmatter contribute only when
    ``working_dir`` matches one of the globs.
    """
    sections: list[str] = []
    legacy = _read_if_exists(USER_RULES_PATH)
    if legacy:
        sections.append(legacy)
    ember_dir = _read_rules_dir_files(USER_RULES_DIR, working_dir, project_dir)
    if ember_dir:
        sections.append(ember_dir)
    if read_claude_rules:
        claude_dir = _read_rules_dir_files(
            CLAUDE_USER_RULES_DIR, working_dir, project_dir
        )
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)


def load_project_rules(project_dir: Path, read_claude_md: bool = True) -> str:
    """Load project root rules (``ember.md`` and/or ``CLAUDE.md``)."""
    return _read_rules_dir(project_dir, _rules_filenames(read_claude_md))


def load_subdirectory_rules(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> list[tuple[str, str]]:
    """Collect rules from subdirectories between project root and working dir.

    Walks from ``working_dir`` up to (but not including) ``project_dir``,
    collecting any rules files found along the way.

    Returns:
        List of (relative_path, content) tuples, ordered shallowest first.
    """
    if working_dir is None:
        return []

    project_dir = project_dir.resolve()
    working_dir = working_dir.resolve()

    # working_dir must be inside project_dir
    try:
        working_dir.relative_to(project_dir)
    except ValueError:
        return []

    results: list[tuple[str, str]] = []
    current = working_dir

    filenames = _rules_filenames(read_claude_md)
    while current != project_dir:
        content = _read_rules_dir(current, filenames)
        if content:
            rel = current.relative_to(project_dir)
            results.append((str(rel), content))
        current = current.parent

    # Return shallowest first (closer to root = earlier in list)
    results.reverse()
    return results


def load_project_context(
    project_dir: Path,
    project_file: str = "ember.md",
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> str:
    """Load and merge all applicable rules into a single context string.

    Checks for ``ember.md`` at every level, and also ``CLAUDE.md`` if
    ``read_claude_md`` is True. Merges rules from three levels:

    1. User-level (``~/.ember/rules.md``, ``~/.ember/rules/*.md``, and —
       when ``read_claude_md`` is set — ``~/.claude/rules/*.md``)
    2. Project root (``ember.md`` and optionally ``CLAUDE.md``)
    3. Subdirectory rules (walking from working_dir up to project root)

    Args:
        project_dir: The project root directory.
        project_file: Kept for config compatibility.
        working_dir: Optional current working subdirectory for subdirectory rules.
        read_claude_md: Whether to also read ``CLAUDE.md`` files and
            ``~/.claude/rules/*.md`` (default True).

    Returns:
        Merged rules string with clear section headers, or empty string if no
        rules files exist.
    """
    sections: list[str] = []

    # 1. User-level rules (legacy file + ember/claude rules directories)
    user = load_user_rules(
        working_dir=working_dir,
        project_dir=project_dir,
        read_claude_rules=read_claude_md,
    )
    if user:
        sections.append(f"# User Rules\n\n{user}")

    # 2. Project root rules
    root = load_project_rules(project_dir, read_claude_md)
    if root:
        sections.append(f"# Project Rules\n\n{root}")

    # 3. Subdirectory rules
    for rel_path, content in load_subdirectory_rules(project_dir, working_dir, read_claude_md):
        sections.append(f"# Directory Rules ({rel_path}/)\n\n{content}")

    return "\n\n---\n\n".join(sections)
