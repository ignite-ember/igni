"""Project-scoped rules — three sources that all read from the
active project's directory tree.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8. Same dependency-injection design as
:mod:`context_managed` and :mod:`context_user`: this module holds
the loaders; the shared rules-reading helpers are passed by argument
so the module stays a leaf in the import graph and CODE_STANDARDS
Rule 2 (no inline imports) holds.

## Sources loaded (in the order the session merges them)

1. **Project root** — ``ember.md`` / ``CLAUDE.md`` at the project
   root, via :func:`load_project_rules`.
2. **Project shared rules dirs** — committed shared rules at
   ``<project>/.ember/rules/*.md`` and (when ``read_claude_md``)
   ``<project>/.claude/rules/*.md``, via
   :func:`load_project_rules_dirs`. Symmetric to the user-level
   directory form, but versioned with the repo so the whole team
   shares the same rule set.
3. **Subdirectory chain** — ``ember.md`` / ``CLAUDE.md`` in any
   parent of the working file, walking up to (but not including)
   the project root, via :func:`load_subdirectory_rules`. Returns a
   list rather than a single string because the session prompt
   groups them with headers per subdirectory.

Files in the ``rules/`` directories may carry YAML frontmatter with
a ``paths:`` glob list. Files whose paths don't match the session's
working directory are skipped — enforced by the injected
``read_rules_dir_files``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def load_project_rules(
    read_rules_dir: Callable[..., str],
    rules_filenames: Callable[[bool], tuple[str, ...]],
    project_dir: Path,
    read_claude_md: bool = True,
) -> str:
    """Load project root rules (``ember.md`` and/or ``CLAUDE.md``).

    Simple one-shot: reads the two canonical files (and their
    ``.local.md`` override siblings, delegated to
    ``read_rules_dir``) from the project root. ``@<path>.md``
    imports resolve against the project root itself.

    Helpers injected — see module docstring.
    """
    return read_rules_dir(
        project_dir,
        rules_filenames(read_claude_md),
        allowed_root=project_dir,
    )


def load_project_rules_dirs(
    read_rules_dir_files: Callable[..., str],
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> str:
    """Load committed shared rules from project-level rules directories.

    Symmetric to the user-level pattern (``~/.ember/rules/`` /
    ``~/.claude/rules/``), but for a single project: a repo can
    commit shared rules at ``<project>/.ember/rules/*.md`` and
    ``<project>/.claude/rules/*.md``. Each file may carry YAML
    frontmatter with a ``paths:`` glob list — files whose paths
    don't match the session's working directory are skipped, same
    rules as the user-level loader.

    ``@<path>.md`` imports inside these files resolve against the
    rules directory itself (not the whole project), matching the
    user-level scoping so a project rules file can't accidentally
    reach into vendored / generated content elsewhere in the repo.

    Helpers injected — see module docstring.
    """
    sections: list[str] = []
    ember_dir = read_rules_dir_files(
        project_dir / ".ember" / "rules",
        working_dir=working_dir,
        project_dir=project_dir,
    )
    if ember_dir:
        sections.append(ember_dir)
    if read_claude_md:
        claude_dir = read_rules_dir_files(
            project_dir / ".claude" / "rules",
            working_dir=working_dir,
            project_dir=project_dir,
        )
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)


def load_subdirectory_rules(
    read_rules_dir: Callable[..., str],
    rules_filenames: Callable[[bool], tuple[str, ...]],
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> list[tuple[str, str]]:
    """Collect rules from subdirectories between project root and working dir.

    Walks from ``working_dir`` up to (but not including)
    ``project_dir``, collecting any rules files found along the way.
    Returns a list rather than a single string because the session
    prompt groups them with per-subdirectory headers.

    Returns:
        List of ``(relative_path, content)`` tuples, ordered
        shallowest-first.

    Helpers injected — see module docstring.
    """
    if working_dir is None:
        return []

    project_dir = project_dir.resolve()
    working_dir = working_dir.resolve()

    # working_dir must be inside project_dir — otherwise we're
    # walking up from a stray file and would climb past the repo.
    try:
        working_dir.relative_to(project_dir)
    except ValueError:
        return []

    results: list[tuple[str, str]] = []
    current = working_dir

    filenames = rules_filenames(read_claude_md)
    while current != project_dir:
        content = read_rules_dir(current, filenames, allowed_root=project_dir)
        if content:
            rel = current.relative_to(project_dir)
            results.append((str(rel), content))
        current = current.parent

    # Return shallowest first (closer to root = earlier in list).
    results.reverse()
    return results
