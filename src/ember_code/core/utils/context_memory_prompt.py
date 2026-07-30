"""Memory writeback prompt — the system-prompt block the agent sees
at session start teaching it how to persist memories.

Extracted from :mod:`context_memory` per CODE_STANDARDS Pattern 8
(one responsibility per file). The parent module owns filesystem
concerns (path resolution, ``MEMORY.md`` reading, dir bootstrap);
this file owns the pure-text template + its renderer.

## Why a separate module

The template is inert markdown data — 79 lines of prose telling the
agent when to save, what memory types exist, what NEVER to save,
and the on-disk file shape. Gluing it into a filesystem module
mixes two concerns (Pattern 8 offender) and makes both harder to
scan.

## Why ``string.Template`` over f-string / ``str.format``

The template body contains literal markdown braces
(``{{memory body ...}}``) inside the fenced YAML frontmatter block.
An f-string would require the caller to know the current binding
scope; ``str.format`` would need every literal ``{`` doubled to
``{{`` throughout. ``string.Template`` with ``$memory_dir`` leaves
every brace untouched — the template body is pure text a designer
can edit without knowing Python quoting rules.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

# ── Template constant ─────────────────────────────────────────────
#
# Kept as a module-level constant so tests / tooling can assert on
# the raw form without instantiating the renderer. The single
# ``$memory_dir`` placeholder is substituted at render time.

MEMORY_WRITEBACK_TEMPLATE: str = """# auto memory

You have a persistent, file-based memory system at `$memory_dir/`. \
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

{memory body — for feedback/project, structure as: rule/fact, then \
**Why:** line, then **How to apply:** line. Link related memories \
with [[their-slug]].}
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


# ── Renderer class ────────────────────────────────────────────────


class MemoryWritebackPrompt:
    """Render the memory-writeback system-prompt block for one project.

    Holds the target memory directory as instance state so the
    constructor names the invariant: you can't render this block
    without knowing where the agent should save. Future extensions
    (a ``render_json`` for structured prompts, a ``validate`` pre-
    render hook) attach here without touching the caller side.
    """

    #: Class attribute so the raw template is discoverable off the
    #: class as well as from module scope. Kept as a
    #: :class:`string.Template` instance so ``substitute`` is a
    #: pre-parsed operation, not a re-parse on every render.
    TEMPLATE: Template = Template(MEMORY_WRITEBACK_TEMPLATE)

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir

    def render(self) -> str:
        """Substitute ``$memory_dir`` and return the rendered block."""
        return self.TEMPLATE.substitute(memory_dir=str(self._memory_dir))
