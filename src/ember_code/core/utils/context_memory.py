"""Per-project auto-memory — MEMORY.md index loading + write-back
instructions the agent needs at session start.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8 (small modules, one responsibility). The
parent module was 778 LoC of hierarchical rules loading with
memory-index handling glued into the middle; this file holds the
memory-index concern only.

Backwards-compatible: the public API (:func:`ensure_memory_dir`,
:func:`memory_writeback_instructions`, :func:`load_memory_index`) is
re-exported by :mod:`context` so existing callers work unchanged.

## What lives here

- **Path resolution** — encode a project dir as a CC-compatible slug,
  resolve the ember-native + Claude Code cross-tool memory dirs.
- **``MEMORY.md`` reading** — with the 200-line / 25-KB cap Claude Code
  publishes, so a runaway memory file can never blow up the session
  prompt.
- **Write-back instructions** — the system-prompt block the agent
  sees at session start, teaching it how to save new memories
  (four types: user / feedback / project / reference; explicit
  DON'T-save categories).
- **Bootstrap** — ``ensure_memory_dir`` creates the per-project
  memory directory idempotently so the agent's first ``save_file``
  doesn't fail on a missing parent.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────

#: Filename of the per-project memory INDEX. Individual memory files
#: are separate ``<name>.md`` files in the same directory; the index
#: is a one-line-per-memory table of contents.
_MEMORY_INDEX_NAME = "MEMORY.md"

#: Prefix line cap Claude Code publishes. Loaded into the session
#: prompt at start; the rest of the file survives untouched but is
#: not visible until the agent explicitly reads it.
_MEMORY_INDEX_MAX_LINES = 200

#: Byte cap that runs alongside the line cap — whichever hits first
#: wins. Matches Claude Code's 25 KB published cap.
_MEMORY_INDEX_MAX_BYTES = 25_000


# ── Path helpers ───────────────────────────────────────────────────


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


# ── Public API ─────────────────────────────────────────────────────


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
