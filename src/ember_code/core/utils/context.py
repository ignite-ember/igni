"""Context utilities — thin public API over :class:`RulesContextLoader`.

Loads rules from several sources, all merged into the session prompt:

0. **Managed policy** — sysadmin-enforced ``ember.md`` / ``CLAUDE.md``
   in a platform-specific write-protected directory (e.g.
   ``/Library/Application Support/Ember/`` on darwin). Prepended
   first so the model sees org-pinned guidance ahead of everything
   else.
0.5. **Memory index** — the agent's per-project ``MEMORY.md`` from
   ``~/.ember/projects/<slug>/memory/`` (or ``~/.claude/projects/
   <slug>/memory/`` as a cross-tool fallback), capped at 200 lines
   / 25 KB.
1. **User-level** — ``~/.ember/rules.md`` (legacy),
   ``~/.ember/rules/*.md`` (dir form), plus ``~/.claude/rules/*.md``
   when cross-tool support is enabled.
2. **Project root** — ``ember.md`` / ``CLAUDE.md`` and their
   ``.local.md`` override siblings.
3. **Project shared rules dirs** — ``<project>/.ember/rules/*.md``
   and (when cross-tool) ``<project>/.claude/rules/*.md``.
4. **Subdirectory** — ``ember.md`` / ``CLAUDE.md`` in any parent of
   the working file, walking up to the project root.

## Architecture

The real work happens in :class:`RulesContextLoader`
(:mod:`context_loader`) — one class that owns
``project_dir`` / ``working_dir`` / ``read_claude_md`` and drives
the six tiers polymorphically. Every function in THIS module is
a thin one-liner that constructs a loader and delegates. Kept as
functions rather than moved to the class so external callers
(``session/core.py``, ``instructions_builder.py``, the test suite)
don't need to touch their imports.

The ``context_<tier>.py`` sibling modules
(``context_managed`` / ``context_user`` / ``context_project`` /
``context_memory`` / ``context_frontmatter`` / ``context_imports`` /
``context_readers``) hold the per-tier logic + primitives — one
responsibility per file per CODE_STANDARDS Pattern 8.
"""

from __future__ import annotations

import logging
from pathlib import Path

# Frontmatter parsing — kept as the legacy private alias so test
# monkeypatches against ``context._parse_frontmatter`` still resolve.
from ember_code.core.utils.context_frontmatter import (  # noqa: F401
    parse_frontmatter as _parse_frontmatter,
)

# ── Legacy re-exports (leading-underscore private helpers) ────────
#
# Extensive test suites + ``rules_index.py`` import these names
# from this module directly. Keeping the re-exports as documented
# scope-limited debt: migrating every call site is a separate
# concern from the OOP refactor happening in this file. The
# audit's target grade (B+) explicitly accounts for these
# surviving one release cycle.
# The coordinator class this facade delegates to. Every wrapper
# below builds one of these and calls a method — no duplicated
# logic here.
from ember_code.core.utils.context_loader import RulesContextLoader

# Managed-policy platform-dir lookup — re-exported here so tests
# can ``monkeypatch.setattr(context, "_platform_managed_rules_dir",
# ...)`` and see the override honoured. The loader's default
# ``platform_dir_fn`` reads THIS module's name at call time (via
# the wrapper below), NOT the ``context_managed`` module's, so the
# monkeypatch reliably takes effect.
from ember_code.core.utils.context_managed import (
    _platform_managed_rules_dir,
)

# Memory subsystem — re-exported for backwards compatibility with
# callers (``session/core.py``, tests) that took the public names
# off this module before the extraction. Private helpers stay here
# too so test monkeypatches against ``context.<name>`` resolve
# against the same module the loader reads from.
from ember_code.core.utils.context_memory import (  # noqa: F401
    ProjectMemoryBank,
    _claude_project_memory_dir,
    _ember_project_memory_dir,
    _project_memory_slug,
    ensure_memory_dir,
    load_memory_index,
    memory_writeback_instructions,
)

# Result schemas — exposed for downstream callers that want the
# typed form instead of the flattened ``str``.
from ember_code.core.utils.context_schemas import (
    RulesBundle,
    RulesSection,
    SubdirectoryRules,
)

# User-rules path constants — same pattern. Tests patch these on
# THIS module (``monkeypatch.setattr(context, "USER_RULES_PATH", ...)``)
# and the loader's ``load_user`` method reads them off ``context``
# at call time.
from ember_code.core.utils.context_user import (
    CLAUDE_USER_RULES_DIR,
    USER_RULES_DIR,
    USER_RULES_PATH,
)

__all__ = [
    "ensure_memory_dir",
    "load_managed_rules",
    "load_memory_index",
    "load_project_context",
    "load_project_rules",
    "load_project_rules_dirs",
    "load_subdirectory_rules",
    "load_user_rules",
    "memory_writeback_instructions",
    "ProjectMemoryBank",
    "RulesBundle",
    "RulesContextLoader",
    "RulesSection",
    "SubdirectoryRules",
    "USER_RULES_PATH",
    "USER_RULES_DIR",
    "CLAUDE_USER_RULES_DIR",
]

logger = logging.getLogger(__name__)


def _make_loader(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> RulesContextLoader:
    """Build a loader with ``platform_dir_fn`` pointing at THIS
    module's ``_platform_managed_rules_dir`` name so monkeypatches
    of that name are honoured at call time.

    A stored ``self.platform_dir_fn = _platform_managed_rules_dir``
    would freeze the reference at loader-construction time. The
    lambda closes over ``globals()`` so
    ``monkeypatch.setattr(context, "_platform_managed_rules_dir",
    lambda: ...)`` mid-test takes effect on the very next
    ``load_managed_rules`` call.
    """
    return RulesContextLoader(
        project_dir=project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
        platform_dir_fn=lambda: _platform_managed_rules_dir(),
    )


def load_user_rules(
    working_dir: Path | None = None,
    project_dir: Path | None = None,
    read_claude_rules: bool = True,
) -> str:
    """Load user-level global rules — thin wrapper around :meth:`RulesContextLoader.load_user`.

    ``project_dir`` is optional here for backwards compatibility
    (some tests call ``load_user_rules()`` with no args). We fall
    back to the working dir or cwd so the loader can still build
    itself.
    """
    anchor = project_dir or working_dir or Path.cwd()
    loader = _make_loader(
        project_dir=anchor,
        working_dir=working_dir,
        read_claude_md=read_claude_rules,
    )
    return loader.load_user()


def load_project_rules(project_dir: Path, read_claude_md: bool = True) -> str:
    """Load project root rules — thin wrapper around :meth:`RulesContextLoader.load_project_root`."""
    return _make_loader(project_dir, read_claude_md=read_claude_md).load_project_root()


def load_managed_rules(read_claude_md: bool = True) -> str:
    """Load the sysadmin-enforced managed-policy instructions file.

    Thin wrapper around :meth:`RulesContextLoader.load_managed`.
    ``project_dir`` is a required loader field but the managed
    tier ignores it — pass ``Path.cwd()`` as a safe anchor.
    """
    return _make_loader(Path.cwd(), read_claude_md=read_claude_md).load_managed()


def load_project_rules_dirs(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> str:
    """Load project shared rules dirs — thin wrapper around :meth:`RulesContextLoader.load_project_dirs`."""
    return _make_loader(
        project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
    ).load_project_dirs()


def load_subdirectory_rules(
    project_dir: Path,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> list[SubdirectoryRules]:
    """Collect rules from subdirectories — thin wrapper around :meth:`RulesContextLoader.load_subdirectory`.

    Return type changed from ``list[tuple[str, str]]`` to
    ``list[SubdirectoryRules]`` in the refactor — Pattern 2 fix
    (structured data with >1 field gets a Pydantic model). Each
    entry exposes ``rel_path`` and ``content`` attributes.
    """
    return _make_loader(
        project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
    ).load_subdirectory()


def load_project_context(
    project_dir: Path,
    project_file: str = "ember.md",
    working_dir: Path | None = None,
    read_claude_md: bool = True,
) -> str:
    """Load and merge all applicable rules into a single context string.

    Thin wrapper around :meth:`RulesContextLoader.load_all`. See
    the class docstring for the six-tier composition order. The
    ``project_file`` argument is retained for config compatibility
    (older callers pass it from settings) but currently unused —
    the loader picks up ``ember.md`` / ``CLAUDE.md`` by convention.
    """
    del project_file  # kept for API compatibility
    return (
        _make_loader(
            project_dir,
            working_dir=working_dir,
            read_claude_md=read_claude_md,
        )
        .load_all()
        .render()
    )
