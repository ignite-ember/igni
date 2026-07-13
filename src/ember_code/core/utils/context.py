"""Context utilities — hierarchical project rules loading.

Loads rules from several sources, all merged into the session prompt:

0. **Managed policy** — sysadmin-enforced ``ember.md`` / ``CLAUDE.md``
   in a platform-specific write-protected directory (e.g.
   ``/Library/Application Support/Ember/`` on darwin). Prepended
   first so the model sees org-pinned guidance ahead of everything
   else. Sibling to the managed-settings file (see
   ``settings._platform_managed_settings_path``).
0.5. **Memory index** — the agent's per-project ``MEMORY.md`` from
   ``~/.ember/projects/<slug>/memory/`` (or ``~/.claude/projects/
   <slug>/memory/`` as a cross-tool fallback). Loaded at session
   start, capped at 200 lines / 25 KB so a runaway memory file
   can't blow up the system prompt. Lets the agent recall what
   it has previously stored about this project before reading
   any user/project rules.
1. **User-level**
   - ``~/.ember/rules.md`` — legacy single-file form
   - ``~/.ember/rules/*.md`` — directory form (one file per topic)
   - ``~/.claude/rules/*.md`` — cross-tool form, gated on ``cross_tool_support``
2. **Project root** — ``ember.md`` / ``CLAUDE.md`` (and their
   ``.local.md`` override siblings) at the project root
3. **Project shared rules dirs** — committed shared rules at
   ``<project>/.ember/rules/*.md`` and (when ``cross_tool_support``)
   ``<project>/.claude/rules/*.md``. Symmetric to the user-level
   pattern, but versioned with the repo so the whole team shares
   the same rule set.
4. **Subdirectory** — ``ember.md`` / ``CLAUDE.md`` in any parent of the
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

# Re-export the memory subsystem for backwards compatibility. The
# actual implementation lives in ``context_memory.py`` (extracted per
# CODE_STANDARDS.md Pattern 8 — small modules, one responsibility).
# Existing callers of ``from ember_code.core.utils.context import
# ensure_memory_dir`` (session/core.py, tests) work unchanged.
from ember_code.core.utils.context_memory import (
    ensure_memory_dir,
    load_memory_index,
    memory_writeback_instructions,
)

# Managed-policy loading — extracted per Pattern 8. Wrapper below
# (``load_managed_rules``) dependency-injects the shared helpers so
# the managed module stays a leaf in the import graph.
from ember_code.core.utils.context_managed import (
    _platform_managed_rules_dir,
    load_managed_rules as _context_managed_load,
)

# User rules — same DI pattern. ``USER_RULES_PATH`` /
# ``USER_RULES_DIR`` / ``CLAUDE_USER_RULES_DIR`` re-exported for
# tests that monkeypatch them.
from ember_code.core.utils.context_user import (
    CLAUDE_USER_RULES_DIR,
    USER_RULES_DIR,
    USER_RULES_PATH,
    load_user_rules as _context_user_load,
)

# Project rules — three loaders (project root file, project
# ``rules/`` dirs, subdirectory walk) all injected the same way.
from ember_code.core.utils.context_project import (
    load_project_rules as _context_project_root_load,
    load_project_rules_dirs as _context_project_dirs_load,
    load_subdirectory_rules as _context_project_subdir_load,
)

# Frontmatter primitives — pure, re-exported under both the public
# and legacy private spellings so ``from context import _parse_frontmatter``
# in ``rules_index.py`` + tests keeps working.
from ember_code.core.utils.context_frontmatter import (
    parse_frontmatter as _parse_frontmatter,
    matches_paths as _matches_paths,
)

# @<path>.md import resolution — pure primitives extracted per
# Pattern 8. Re-exported under the legacy leading-underscore names
# so ``rules_index.py`` (which imports ``_resolve_imports``) works
# unchanged.
from ember_code.core.utils.context_imports import (
    IMPORT_MAX_DEPTH as _IMPORT_MAX_DEPTH,
    AT_IMPORT_RE as _AT_IMPORT_RE,
    FENCED_BLOCK_RE as _FENCED_BLOCK_RE,
    INLINE_CODE_RE as _INLINE_CODE_RE,
    CODE_SENTINEL_RE as _CODE_SENTINEL_RE,
    resolve_at_path as _resolve_at_path,
    mask_code_regions as _mask_code_regions,
    unmask_code_regions as _unmask_code_regions,
    resolve_imports as _resolve_imports,
)

# Shared file-read helpers — extracted per Pattern 8, all
# defensive (missing paths / read errors → ``""``). Legacy leading-
# underscore names re-exported for callers who took the private
# spelling from this module.
from ember_code.core.utils.context_readers import (
    read_if_exists as _read_if_exists,
    read_rules_dir as _read_rules_dir,
    read_rules_dir_files as _read_rules_dir_files,
    read_with_imports as _read_with_imports,
    rules_filenames as _rules_filenames,
)

# Also re-export the private helpers — existing test suites import
# these directly (they date from before the extraction) and it's
# cheaper to keep the imports working than to touch every test.
from ember_code.core.utils.context_memory import (
    _claude_project_memory_dir,
    _ember_project_memory_dir,
    _project_memory_slug,
    _read_memory_index,
    _MEMORY_INDEX_MAX_BYTES,
    _MEMORY_INDEX_MAX_LINES,
    _MEMORY_INDEX_NAME,
)

__all__ = [
    "ensure_memory_dir",
    "load_memory_index",
    "memory_writeback_instructions",
]

logger = logging.getLogger(__name__)

# USER_RULES_PATH / USER_RULES_DIR / CLAUDE_USER_RULES_DIR are
# imported at the top of this module from ``context_user`` — kept
# as top-level names here so ``monkeypatch.setattr(context, "USER_RULES_PATH", ...)``
# still works for tests that were written before the extraction.


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

# ``@<path>.md`` import resolution moved to ``context_imports.py``.
# The regex constants and depth cap are re-exported below for any
# caller that needs them (see ``rules_index.py``).


# ``_parse_frontmatter`` and ``_matches_paths`` moved to
# ``context_frontmatter.py`` — re-exported below at import-time so
# existing callers (``rules_index.py``, tests) work unchanged. The
# aliased ``_``-prefixed names preserve the "private helper" contract
# even though the extracted module exposes the public spellings.


# All five read helpers moved to ``context_readers.py``.
# Re-exported at the top of this module under the legacy leading-
# underscore names so external callers work unchanged.


def load_user_rules(
    working_dir: Path | None = None,
    project_dir: Path | None = None,
    read_claude_rules: bool = True,
) -> str:
    """Load user-level global rules — thin wrapper around
    :func:`ember_code.core.utils.context_user.load_user_rules`.

    Public signature unchanged; the actual sources + concatenation
    live in ``context_user.py`` per CODE_STANDARDS Pattern 8. We
    inject:
    - shared helpers (``_read_with_imports``, ``_read_rules_dir_files``)
    - the path constants THIS module knows about, read at call time
      so ``monkeypatch.setattr(context, "USER_RULES_PATH", ...)``
      still overrides the effective path.
    """
    return _context_user_load(
        _read_with_imports,
        _read_rules_dir_files,
        working_dir=working_dir,
        project_dir=project_dir,
        read_claude_rules=read_claude_rules,
        user_rules_path=USER_RULES_PATH,
        user_rules_dir=USER_RULES_DIR,
        claude_user_rules_dir=CLAUDE_USER_RULES_DIR,
    )


def load_project_rules(project_dir: Path, read_claude_md: bool = True) -> str:
    """Load project root rules — thin wrapper around
    :func:`ember_code.core.utils.context_project.load_project_rules`.
    """
    return _context_project_root_load(
        _read_rules_dir,
        _rules_filenames,
        project_dir=project_dir,
        read_claude_md=read_claude_md,
    )


def load_managed_rules(read_claude_md: bool = True) -> str:
    """Load the sysadmin-enforced managed-policy instructions file.

    Thin wrapper around
    :func:`ember_code.core.utils.context_managed.load_managed_rules`
    — the actual logic moved there per CODE_STANDARDS Pattern 8. We
    inject the three helpers so the managed module stays a leaf in
    the import graph.

    ``platform_dir_fn`` lambda reads
    ``_platform_managed_rules_dir`` off THIS module's namespace at
    call time, so tests can `monkeypatch.setattr(context,
    "_platform_managed_rules_dir", ...)` and see the override
    even after the extraction.

    Public signature unchanged; existing callers and tests work
    without modification.
    """
    return _context_managed_load(
        _read_rules_dir,
        _rules_filenames,
        platform_dir_fn=lambda: _platform_managed_rules_dir(),
        read_claude_md=read_claude_md,
    )


def load_project_rules_dirs(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> str:
    """Load project rules directories — thin wrapper around
    :func:`ember_code.core.utils.context_project.load_project_rules_dirs`.
    """
    return _context_project_dirs_load(
        _read_rules_dir_files,
        project_dir=project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
    )


def load_subdirectory_rules(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> list[tuple[str, str]]:
    """Collect rules from subdirectories — thin wrapper around
    :func:`ember_code.core.utils.context_project.load_subdirectory_rules`.
    """
    return _context_project_subdir_load(
        _read_rules_dir,
        _rules_filenames,
        project_dir=project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
    )


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

    # 0. Managed policy (sysadmin-enforced) — appears FIRST so the
    #    model encounters org-pinned guidance before any user or
    #    project rules. The file lives in a write-protected OS
    #    location, so this section can't be defeated by editing
    #    user/project files.
    managed = load_managed_rules(read_claude_md=read_claude_md)
    if managed:
        sections.append(f"# Managed Policy\n\n{managed}")

    # 0.5. Memory index — the agent's persistent scratch from
    #      prior conversations. Loaded after the managed policy
    #      (which is non-negotiable) but before user/project
    #      rules, so anything the agent has previously remembered
    #      about this project shapes how it interprets the rules
    #      that follow. 200-line / 25-KB cap is enforced inside
    #      ``load_memory_index``.
    memory_index = load_memory_index(project_dir, read_claude_memory=read_claude_md)
    if memory_index:
        sections.append(f"# Memory Index\n\n{memory_index}")

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

    # 3. Project-level shared rules dirs
    #    (``<project>/.ember/rules/`` / ``<project>/.claude/rules/``)
    project_dirs = load_project_rules_dirs(
        project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
    )
    if project_dirs:
        sections.append(f"# Project Shared Rules\n\n{project_dirs}")

    # 4. Subdirectory rules
    for rel_path, content in load_subdirectory_rules(project_dir, working_dir, read_claude_md):
        sections.append(f"# Directory Rules ({rel_path}/)\n\n{content}")

    return "\n\n---\n\n".join(sections)
