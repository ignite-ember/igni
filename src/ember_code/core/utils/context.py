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

logger = logging.getLogger(__name__)

USER_RULES_PATH = Path.home() / ".ember" / "rules.md"
USER_RULES_DIR = Path.home() / ".ember" / "rules"
CLAUDE_USER_RULES_DIR = Path.home() / ".claude" / "rules"


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

# Claude Code-style ``@<path>.md`` imports — when a rules file
# contains this token, the loader inlines the referenced file's
# contents. Match shape: ``@`` followed by non-whitespace, non-``)``
# (so links like ``[label](./foo.md)`` aren't accidentally chewed
# up), ending in ``.md`` to keep the token specific.
_AT_IMPORT_RE = re.compile(r"@([^\s)]+\.md)")

# How many nested ``@<path>`` levels we'll follow. Four matches
# Claude Code's documented limit, which is just enough for the
# natural pattern ``CLAUDE.md → @./conventions.md → @./style.md →
# @./detail.md`` without letting cycles or accidental fan-outs
# expand unbounded. Adjust in lockstep with the depth-cap test in
# ``test_context.py`` if you ever change this.
_IMPORT_MAX_DEPTH = 4

# Fenced code blocks — ``` or ~~~ runs (≥3 chars), optionally
# indented up to 3 spaces, with a matching closing fence on its
# own line. Multiline + DOTALL so the body can span lines. We
# match closing fence to be EXACTLY the same string as the open
# fence (not "≥ open" as strict CommonMark allows) — covers the
# overwhelmingly common case where opener and closer are the
# same length.
_FENCED_BLOCK_RE = re.compile(
    r"^[ ]{0,3}(```+|~~~+)[^\n]*\n"
    r".*?"
    r"(?:^[ ]{0,3}\1[ \t]*(?:\n|$))",
    re.MULTILINE | re.DOTALL,
)
# Inline code spans — backtick-delimited, on a single line.
# Permissive on the fence length (any run of ``` `s, any non-
# backtick / non-newline content, any run of ``` `s); over-
# masking is harmless here because the only consequence is
# leaving ``@`` tokens unresolved, which is what we want for
# code anyway.
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
# Sentinel marker used while masking — NUL bytes don't appear in
# normal text, so the placeholder is unambiguous.
_CODE_SENTINEL_RE = re.compile(r"\0CODE(\d+)\0")


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


def _matches_paths(paths: list[str], working_dir: Path | None, project_dir: Path | None) -> bool:
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


def _resolve_at_path(token: str, source_path: Path, allowed_root: Path) -> Path | None:
    """Translate one ``@<token>`` into an absolute path under
    ``allowed_root``. Returns ``None`` when the token doesn't
    resolve to an existing file inside the root — the caller
    leaves the literal token in place in that case."""
    try:
        if token.startswith("~"):
            candidate = Path(token).expanduser()
        elif token.startswith("/"):
            candidate = Path(token)
        else:
            candidate = source_path.parent / token
        candidate = candidate.resolve()
        candidate.relative_to(allowed_root.resolve())
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _mask_code_regions(content: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks and inline code spans with NUL-
    delimited sentinels so a later ``@`` substitution pass doesn't
    touch their contents. Returns ``(masked, originals)`` where
    ``originals[i]`` is the substring replaced by sentinel ``i``.

    Fenced blocks are masked first (multiline), then inline
    code spans on whatever's left. Over-masking is acceptable —
    the only effect is leaving ``@`` tokens inside the masked
    region as literals, which is exactly what we want for code.
    """
    originals: list[str] = []

    def stash(m: re.Match[str]) -> str:
        idx = len(originals)
        originals.append(m.group(0))
        return f"\0CODE{idx}\0"

    masked = _FENCED_BLOCK_RE.sub(stash, content)
    masked = _INLINE_CODE_RE.sub(stash, masked)
    return masked, originals


def _unmask_code_regions(content: str, originals: list[str]) -> str:
    """Reverse of ``_mask_code_regions`` — restore stashed code
    regions identified by the ``\\0CODE<idx>\\0`` sentinels."""

    def restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(originals):
            return originals[idx]
        return m.group(0)

    return _CODE_SENTINEL_RE.sub(restore, content)


def _resolve_imports(
    content: str,
    source_path: Path,
    allowed_root: Path,
    seen: set[Path] | None = None,
    depth: int = 0,
) -> str:
    """Inline ``@<path>.md`` imports in ``content``.

    Recursive, capped at ``_IMPORT_MAX_DEPTH``. Cycle-safe via
    ``seen`` — once a file has been inlined in this resolution
    chain, a later ``@`` referencing it leaves the literal token
    so the agent can see the unresolved reference instead of
    looping. Imports escaping ``allowed_root`` (e.g. a project
    file importing ``@/etc/passwd``) are also left as literals.

    Tokens inside code spans (`` `…` ``) and fenced code blocks
    (```` ``` ```` / ``~~~``) are deliberately NOT inlined — they're
    masked out before the substitution pass and restored after,
    so rules files can document ``@<path>.md`` syntax without
    triggering accidental imports.
    """
    if depth >= _IMPORT_MAX_DEPTH:
        return content
    if seen is None:
        seen = set()

    def replacer(m: re.Match[str]) -> str:
        token = m.group(1)
        resolved = _resolve_at_path(token, source_path, allowed_root)
        if resolved is None or resolved in seen:
            return m.group(0)
        try:
            inner = resolved.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("@ import read %s failed: %s", resolved, exc)
            return m.group(0)
        seen.add(resolved)
        return _resolve_imports(inner, resolved, allowed_root, seen, depth + 1)

    masked, originals = _mask_code_regions(content)
    substituted = _AT_IMPORT_RE.sub(replacer, masked)
    return _unmask_code_regions(substituted, originals)


def _read_with_imports(path: Path, allowed_root: Path) -> str:
    """Read a rules file and inline any ``@<path>.md`` references."""
    content = _read_if_exists(path)
    if not content:
        return ""
    return _resolve_imports(content, path, allowed_root)


def _read_rules_dir_files(
    directory: Path,
    working_dir: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Concatenate all ``*.md`` files in a rules directory, respecting ``paths:``.

    ``@<path>.md`` imports inside each file resolve against
    ``directory`` — keeps user-level rules from reaching into the
    project (and vice versa).
    """
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        content = _read_with_imports(path, allowed_root=directory)
        if not content:
            continue
        paths_filter, body = _parse_frontmatter(content)
        if not _matches_paths(paths_filter, working_dir, project_dir):
            continue
        body = body.strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def _rules_filenames(read_claude_md: bool = True) -> tuple[str, ...]:
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


def _read_rules_dir(
    directory: Path,
    filenames: tuple[str, ...] = ("ember.md", "CLAUDE.md"),
    allowed_root: Path | None = None,
) -> str:
    """Read rules from a directory, checking all candidate filenames.

    Returns concatenated contents of all found files. When
    ``allowed_root`` is provided, ``@<path>.md`` imports resolve
    inside it (otherwise the file's own directory is the root, which
    is fine for the user-rules dir-form callers).
    """
    root = allowed_root if allowed_root is not None else directory
    parts: list[str] = []
    for name in filenames:
        content = _read_with_imports(directory / name, allowed_root=root)
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
    legacy = _read_with_imports(USER_RULES_PATH, allowed_root=USER_RULES_PATH.parent)
    if legacy:
        sections.append(legacy)
    ember_dir = _read_rules_dir_files(USER_RULES_DIR, working_dir, project_dir)
    if ember_dir:
        sections.append(ember_dir)
    if read_claude_rules:
        claude_dir = _read_rules_dir_files(CLAUDE_USER_RULES_DIR, working_dir, project_dir)
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)


def load_project_rules(project_dir: Path, read_claude_md: bool = True) -> str:
    """Load project root rules (``ember.md`` and/or ``CLAUDE.md``)."""
    return _read_rules_dir(
        project_dir,
        _rules_filenames(read_claude_md),
        allowed_root=project_dir,
    )


def _platform_managed_rules_dir() -> Path | None:
    """OS-specific directory that may host a sysadmin-enforced
    instructions file (``ember.md`` and/or ``CLAUDE.md``).

    Sibling to the managed-settings file — both live in the same
    write-protected parent so a sysadmin / MDM profile can drop a
    full policy bundle (settings + instructions) in one place.
    Returns ``None`` on unknown platforms; the loader treats that
    as "no managed instructions tier."
    """
    import sys

    if sys.platform == "darwin":
        return Path("/Library/Application Support/Ember")
    if sys.platform.startswith("linux"):
        return Path("/etc/ember")
    if sys.platform == "win32":
        import os

        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "Ember"
    return None


_MEMORY_INDEX_NAME = "MEMORY.md"
_MEMORY_INDEX_MAX_LINES = 200
_MEMORY_INDEX_MAX_BYTES = 25_000  # 25 KB, matches Claude Code's published cap.


def _project_memory_slug(project_dir: Path) -> str:
    """Encode an absolute project path as a directory-safe slug.

    Mirrors Claude Code's convention: the absolute path with every
    ``/`` replaced by ``-`` (and the leading ``/`` becomes a
    leading ``-``). So ``/Users/x/proj`` → ``-Users-x-proj``.
    Matching CC's encoding means a user who already has a CC
    memory bank for this repo automatically lights up the cross-
    tool fallback below — no migration step required.
    """
    return str(project_dir.resolve()).replace("/", "-")


def _ember_project_memory_dir(project_dir: Path) -> Path:
    """Ember-native per-project memory dir
    (``~/.ember/projects/<slug>/memory/``)."""
    return Path.home() / ".ember" / "projects" / _project_memory_slug(project_dir) / "memory"


def _claude_project_memory_dir(project_dir: Path) -> Path:
    """Claude Code's per-project memory dir
    (``~/.claude/projects/<slug>/memory/``). Read only — we never
    write here."""
    return Path.home() / ".claude" / "projects" / _project_memory_slug(project_dir) / "memory"


def _read_memory_index(memory_dir: Path) -> str:
    """Read ``MEMORY.md`` from a memory dir, applying the 200-line /
    25-KB cap. Returns ``""`` when the file doesn't exist or can't
    be read."""
    index_path = memory_dir / _MEMORY_INDEX_NAME
    try:
        if not index_path.is_file():
            return ""
        content = index_path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    lines = content.splitlines(keepends=True)
    if len(lines) > _MEMORY_INDEX_MAX_LINES:
        lines = lines[:_MEMORY_INDEX_MAX_LINES]
    text = "".join(lines)
    if len(text.encode("utf-8")) > _MEMORY_INDEX_MAX_BYTES:
        # Byte cap kicks in second — trim the trailing partial
        # text. ``errors="ignore"`` drops any byte chopped mid-
        # codepoint so we never emit invalid UTF-8.
        text = text.encode("utf-8")[:_MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


def ensure_memory_dir(project_dir: Path) -> Path:
    """Create the per-project memory directory if missing and
    return its path. Called at session bootstrap so the agent's
    first ``save_file`` into the memory area doesn't fail on a
    "parent directory doesn't exist" error.

    The function is idempotent — existing directories are left
    alone, and any OS-level permission error is logged + swallowed
    so a flaky disk doesn't break session boot."""
    target = _ember_project_memory_dir(project_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("ensure_memory_dir %s failed: %s", target, exc)
    return target


def memory_writeback_instructions(project_dir: Path) -> str:
    """Return the system-prompt block that teaches the agent
    how to WRITE new memory entries during a conversation
    (Claude Code parity, row 61).

    Mirrors CC's auto-memory convention: per-project memory dir,
    individual ``<name>.md`` files with YAML frontmatter, an
    index file (``MEMORY.md``) the agent updates alongside.
    The block names the four memory types (user / feedback /
    project / reference), gives concrete WHEN-to-save triggers,
    and lists the categories the agent should NEVER save (code
    patterns, paths, git history — anything derivable from the
    code itself)."""
    memory_dir = _ember_project_memory_dir(project_dir)
    return f"""# auto memory

You have a persistent, file-based memory system at `{memory_dir}/`. \
This directory already exists — write to it directly with the standard \
`save_file` / `edit_file` tools (do not run mkdir or check for its existence).

Build up this memory system over time so future conversations have a \
complete picture of who the user is, how they'd like to collaborate, \
what behaviours to avoid or repeat, and the context behind the work.

If the user explicitly asks you to remember something, save it immediately \
as whichever type fits best. If they ask you to forget something, find and \
remove the relevant entry.

## Types of memory

* `user` — the user's role, goals, responsibilities, knowledge. Lets you \
  tailor future behaviour to their perspective.
* `feedback` — corrections AND confirmations on how to approach work. \
  Both directions matter — recording only corrections drifts you away \
  from approaches the user already validated. Lead with the rule; \
  follow with **Why:** and **How to apply:** lines.
* `project` — ongoing work, goals, initiatives, bugs, deadlines that \
  aren't derivable from the code or git history. Convert relative \
  dates to absolute when saving (today → 2026-06-26).
* `reference` — pointers to where information lives in external systems \
  (Linear, Slack, Grafana dashboards, etc.).

## What NOT to save

* Code patterns, conventions, architecture, file paths, or project \
  structure — these are derivable by reading the project.
* Git history, recent changes, blame info — `git log` / `git blame` \
  are authoritative.
* Debugging recipes or fix details — the fix is in the code; the \
  commit message has the context.
* Anything already documented in `CLAUDE.md` / `ember.md`.
* Ephemeral task details: in-progress work, conversation context.

These exclusions apply even when the user explicitly asks you to save. \
If they ask to save a PR list or activity summary, ask what was *surprising* \
or *non-obvious* about it — that is the part worth keeping.

## How to save memories

Two-step process:

**Step 1** — write the memory to its own file (e.g. `user_role.md`, \
`feedback_testing.md`):

```markdown
---
name: short-kebab-case-slug
description: one-line summary — used to decide relevance in future \
conversations, so be specific
metadata:
  type: user | feedback | project | reference
---

{{memory body — for feedback/project, structure as: rule/fact, then \
**Why:** line, then **How to apply:** line. Link related memories \
with [[their-slug]].}}
```

**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is \
an index, not a memory. Each line: `- [Title](file.md) — one-line hook`. \
No frontmatter on `MEMORY.md`. Keep entries under ~150 characters; the \
first 200 lines load into context, so density matters.

## When to access vs write memory

Read existing memories when they seem relevant or the user references \
prior-conversation work. Write new ones when you learn something — \
don't wait to be told. Update or remove memories that turn out to be \
wrong or outdated.

Before recommending something from memory: if it names a file or symbol, \
verify the file still exists / the symbol still exists. Memory captures \
state at a point in time."""


def load_memory_index(project_dir: Path, read_claude_memory: bool = True) -> str:
    """Load the per-project ``MEMORY.md`` index for the session.

    Tries the ember-native location first
    (``~/.ember/projects/<slug>/memory/MEMORY.md``); when
    ``read_claude_memory`` is set and no ember-native file exists,
    falls back to the equivalent CC path so a user mid-migration
    keeps their existing memory bank without copying files. The
    first 200 lines OR 25 KB (whichever cap hits first) load into
    context — the same prefix budget Claude Code applies — so a
    runaway memory file can never blow up the session prompt.
    """
    text = _read_memory_index(_ember_project_memory_dir(project_dir))
    if text:
        return text
    if read_claude_memory:
        text = _read_memory_index(_claude_project_memory_dir(project_dir))
        if text:
            return text
    return ""


def load_managed_rules(read_claude_md: bool = True) -> str:
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
    """
    managed_dir = _platform_managed_rules_dir()
    if managed_dir is None:
        return ""
    try:
        if not managed_dir.is_dir():
            return ""
    except OSError:
        return ""
    return _read_rules_dir(
        managed_dir,
        _rules_filenames(read_claude_md),
        allowed_root=managed_dir,
    )


def load_project_rules_dirs(
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
    """
    sections: list[str] = []
    ember_dir = _read_rules_dir_files(
        project_dir / ".ember" / "rules",
        working_dir=working_dir,
        project_dir=project_dir,
    )
    if ember_dir:
        sections.append(ember_dir)
    if read_claude_md:
        claude_dir = _read_rules_dir_files(
            project_dir / ".claude" / "rules",
            working_dir=working_dir,
            project_dir=project_dir,
        )
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)


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
        content = _read_rules_dir(current, filenames, allowed_root=project_dir)
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
