"""Typed return schemas for the rules-context loading pipeline.

Sibling to :mod:`context.py` and the ``context_<tier>.py`` leaf
modules. Matches the ``<name>_schemas.py`` convention already in
use elsewhere in the codebase (``audit_schemas.py``,
``display_schemas.py``, ``media_schemas.py``,
``update_checker_schemas.py``) — one file per concern for the
typed data models, adjacent to the module that owns the behaviour.

## What lives here

- :class:`RulesSection` — a single labelled block of rules text
  (``# Managed Policy`` / ``# User Rules`` / …) that the loader
  emits. Owns its own :meth:`RulesSection.render` so the exact
  ``# {heading}\\n\\n{body}`` string is defined once, not
  duplicated by every tier.
- :class:`RulesBundle` — an ordered list of :class:`RulesSection`.
  Represents the full ``load_project_context`` output before it
  gets flattened to a string. Owns :meth:`RulesBundle.render`
  which joins the sections with the ``\\n\\n---\\n\\n`` divider
  that the model consumes.
- :class:`SubdirectoryRules` — one entry from the subdirectory
  walk (``ember.md`` / ``CLAUDE.md`` found in a subdirectory
  between the working dir and the project root). Replaces the
  raw ``list[tuple[str, str]]`` return that ``load_subdirectory_rules``
  used to have — Pattern 2 fix (structured data with >1 field
  gets a Pydantic model).
- Protocol classes for every helper the loader is willing to
  accept as a dependency. Closes the ``Callable[..., str]``
  AP5 violation at both the facade layer and the six sibling
  ``context_<tier>.py`` modules.

All models are immutable (``model_config = ConfigDict(frozen=True)``)
— the loader produces them and hands them off; downstream code
never mutates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

# ── Data schemas ────────────────────────────────────────────────────


class RulesSection(BaseModel):
    """One labelled section in the merged rules block.

    ``heading`` is the human-readable label the model sees
    (e.g. ``"Managed Policy"``, ``"User Rules"``); ``body`` is
    the concatenated rule text. Rendering prepends ``# `` to the
    heading and separates it from the body with a blank line —
    the exact shape ``load_project_context`` produced before the
    refactor, pinned by ``test_managed_section_appears_first``
    and ``test_load_project_context_includes_shared_rules_section``.
    """

    model_config = ConfigDict(frozen=True)

    heading: str
    body: str

    def render(self) -> str:
        """Render as ``# <heading>\\n\\n<body>``."""
        return f"# {self.heading}\n\n{self.body}"


class SubdirectoryRules(BaseModel):
    """One rules file found while walking from working-dir up to the project root.

    ``rel_path`` is the directory path relative to the project
    root (e.g. ``"src/auth"``); ``content`` is the concatenated
    rules text found in that directory. The loader keeps these
    ordered shallowest-first so the model reads the more general
    rules before the more specific ones.
    """

    model_config = ConfigDict(frozen=True)

    rel_path: str
    content: str

    def to_section(self) -> RulesSection:
        """Convert to a :class:`RulesSection` with a labelled heading.

        Matches the pre-refactor ``# Directory Rules (<path>/)``
        header shape so external assertions on that literal still
        pass (see ``TestLoadProjectContext::test_merges_root_and_subdirectory``).
        """
        return RulesSection(
            heading=f"Directory Rules ({self.rel_path}/)",
            body=self.content,
        )


class RulesBundle(BaseModel):
    """The full ordered set of rules sections for a session.

    Produced by :meth:`RulesContextLoader.load_all`. The
    ``sections`` list already carries the six tiers in the exact
    order the session prompt expects (managed → memory → user →
    project → project-shared → subdirectory chain). Rendering
    joins them with the same divider the pre-refactor loader
    used.
    """

    model_config = ConfigDict(frozen=True)

    sections: list[RulesSection]

    def render(self) -> str:
        """Join sections with ``\\n\\n---\\n\\n`` to produce the final string."""
        return "\n\n---\n\n".join(section.render() for section in self.sections)


# ── Protocols for injected helpers ──────────────────────────────────
#
# Every reader we hand into a tier is described by a Protocol so
# the AP5 ``Callable[..., str]`` violation (opaque signatures, no
# way to enforce parameter shape at call sites) is closed at the
# signature layer.


class ReadIfExists(Protocol):
    """Read a file if it exists, returning ``""`` on any failure."""

    def __call__(self, path: Path) -> str: ...


class ReadWithImports(Protocol):
    """Read a rules file and inline its ``@<path>.md`` references."""

    def __call__(self, path: Path, allowed_root: Path) -> str: ...


class RulesFilenames(Protocol):
    """Return the canonical filenames to look for in a rules directory."""

    def __call__(self, read_claude_md: bool = True) -> tuple[str, ...]: ...


class ReadRulesDir(Protocol):
    """Read every canonical rules filename from a directory."""

    def __call__(
        self,
        directory: Path,
        filenames: tuple[str, ...] = ("ember.md", "CLAUDE.md"),
        allowed_root: Path | None = None,
    ) -> str: ...


class ReadRulesDirFiles(Protocol):
    """Read every ``*.md`` inside a rules directory, honoring ``paths:`` frontmatter."""

    def __call__(
        self,
        directory: Path,
        working_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> str: ...


class PlatformDirFn(Protocol):
    """Return the platform-specific managed-rules directory (or ``None``)."""

    def __call__(self) -> Path | None: ...
