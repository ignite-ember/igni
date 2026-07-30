"""Coordinator classes for the rules-context loading pipeline.

Extracted from :mod:`context.py` so that file can stay a thin
public-API facade (Pattern 8: one responsibility per file). This
module holds the OOP guts: :class:`RulesReaders` (typed
composition of the five file-read helpers), :class:`RulesTier`
(a Protocol every tier honours), the six concrete tier classes,
and :class:`RulesContextLoader` (the coordinator that owns
``project_dir`` / ``working_dir`` / ``read_claude_md`` and
iterates the tiers via polymorphism).

## Why this shape

The pre-refactor facade was a 341-line procedural module with:

* Six free-function wrappers, all taking the same
  ``project_dir`` / ``working_dir`` / ``read_claude_md`` triple —
  a classic "implicit subject" cluster (Rule 3 offender).
* A six-branch dispatch inside ``load_project_context`` that
  hand-rolled the tier ordering — the dispatch-dict shape the
  audit flags (should be polymorphism).
* Five ``Callable[..., str]`` parameters threaded through every
  sibling module — AP5 opaque-signature violation.
* Return types of raw ``str`` and ``list[tuple[str, str]]`` for
  structured data — Rule 1 / Pattern 2 violation.

This file fixes all four:

* The implicit subject becomes :class:`RulesContextLoader`'s
  instance state.
* The dispatch becomes a class-level tuple of tier instances
  that ``load_all`` iterates.
* The callable soup becomes a :class:`RulesReaders` composition
  object plus Protocol-typed constructor args in
  :mod:`context_schemas`.
* The return types become
  :class:`context_schemas.RulesBundle` / :class:`RulesSection` /
  :class:`SubdirectoryRules`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

from ember_code.core.utils.context_managed import (
    _platform_managed_rules_dir,
    load_managed_rules,
)
from ember_code.core.utils.context_memory import ProjectMemoryBank
from ember_code.core.utils.context_project import (
    load_project_rules,
    load_project_rules_dirs,
    load_subdirectory_rules,
)
from ember_code.core.utils.context_readers import (
    read_if_exists,
    read_rules_dir,
    read_rules_dir_files,
    read_with_imports,
    rules_filenames,
)
from ember_code.core.utils.context_schemas import (
    PlatformDirFn,
    ReadIfExists,
    ReadRulesDir,
    ReadRulesDirFiles,
    ReadWithImports,
    RulesBundle,
    RulesFilenames,
    RulesSection,
    SubdirectoryRules,
)
from ember_code.core.utils.context_user import (
    load_user_rules,
)

# ── Reader composition ─────────────────────────────────────────────


@dataclass(frozen=True)
class RulesReaders:
    """Typed composition of the five file-read helpers used across every tier.

    Bundling the readers into one object closes the AP5
    ``Callable[..., str]`` violation the pre-refactor facade
    carried in six sibling-module signatures. Every helper is
    typed via a Protocol in :mod:`context_schemas` — the tier
    classes only see well-typed methods, never opaque
    callables.

    Defaults point at the real implementations in
    :mod:`context_readers` and :mod:`context_managed`, so most
    call sites just do ``RulesReaders()``. Tests that want to
    stub a specific helper pass overrides by keyword.
    """

    read_if_exists: ReadIfExists = field(default=read_if_exists)
    read_with_imports: ReadWithImports = field(default=read_with_imports)
    rules_filenames: RulesFilenames = field(default=rules_filenames)
    read_rules_dir: ReadRulesDir = field(default=read_rules_dir)
    read_rules_dir_files: ReadRulesDirFiles = field(default=read_rules_dir_files)


# ── Tier protocol + concrete tiers ─────────────────────────────────


class RulesTier(Protocol):
    """One rules-loading tier the loader knows how to consult.

    Every tier exposes a ``heading`` (what section header it
    contributes when it has content) and a ``load`` method that
    returns the raw rules text. The loader iterates tiers in
    order, skipping any that yield an empty string.
    """

    heading: str

    def load(self, loader: RulesContextLoader) -> str: ...


class ManagedTier:
    """Sysadmin-enforced managed policy (loaded first)."""

    heading = "Managed Policy"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_managed()


class MemoryTier:
    """Per-project ``MEMORY.md`` index (loaded after managed, before user)."""

    heading = "Memory Index"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_memory()


class UserTier:
    """User-level global rules (``~/.ember/rules.md`` / ``~/.ember/rules/`` / ``~/.claude/rules/``)."""

    heading = "User Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_user()


class ProjectRootTier:
    """Project-root ``ember.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""

    heading = "Project Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_project_root()


class ProjectDirsTier:
    """Committed shared rules at ``<project>/.ember/rules/`` and ``<project>/.claude/rules/``."""

    heading = "Project Shared Rules"

    def load(self, loader: RulesContextLoader) -> str:
        return loader.load_project_dirs()


# ── The coordinator ────────────────────────────────────────────────


@dataclass
class RulesContextLoader:
    """Owns the session-level state for a rules load + drives all six tiers.

    Instance state (≤5 fields, per CODE_STANDARDS checklist):

    * ``project_dir`` — the repo root; anchors every tier.
    * ``working_dir`` — optional; drives the subdirectory walk
      and ``paths:`` frontmatter filtering.
    * ``read_claude_md`` — cross-tool toggle (all Claude Code
      variants gated on this).
    * ``readers`` — composed :class:`RulesReaders` object holding
      the injected file-read helpers. Not a raw callable soup;
      see the AP5 rationale in the module docstring.
    * ``platform_dir_fn`` — dynamic lookup for the managed-rules
      directory. Threaded as a callable (not a stored ``Path``)
      so tests that monkeypatch the module-level lookup on
      :mod:`context` (or :mod:`context_managed`) at call time
      see their override honoured. Autouse fixtures rely on
      this — see ``test_context.py::_isolate_managed_rules``.

    ``USER_RULES_PATH`` / ``USER_RULES_DIR`` /
    ``CLAUDE_USER_RULES_DIR`` are deliberately NOT stored on
    the instance — they're read off the :mod:`context` module
    namespace inside :meth:`load_user` at call time so
    ``monkeypatch.setattr(context, "USER_RULES_PATH", ...)``
    keeps working after the extraction. The alternative
    (storing them on ``self``) would silently break every
    hermetic user-rules test in the suite.
    """

    project_dir: Path
    working_dir: Path | None = None
    read_claude_md: bool = True
    readers: RulesReaders = field(default_factory=RulesReaders)
    platform_dir_fn: PlatformDirFn = field(default=_platform_managed_rules_dir)

    # Ordered tuple of tier objects. Iteration order in
    # ``load_all`` matches the order the session prompt expects:
    # managed → memory → user → project root → project shared →
    # subdirectory chain. Adding a tier is one line here plus a
    # method below — no dispatch dict to keep in sync.
    TIERS: ClassVar[tuple[RulesTier, ...]] = (
        ManagedTier(),
        MemoryTier(),
        UserTier(),
        ProjectRootTier(),
        ProjectDirsTier(),
    )

    # ── Per-tier loaders ─────────────────────────────────────

    def load_managed(self) -> str:
        """Sysadmin-enforced managed-policy instructions file."""
        return load_managed_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            platform_dir_fn=self.platform_dir_fn,
            read_claude_md=self.read_claude_md,
        )

    def load_memory(self) -> str:
        """Per-project ``MEMORY.md`` index (200-line / 25-KB cap).

        Uses the OOP-native :class:`ProjectMemoryBank` form
        directly — the free-function shim in :mod:`context_memory`
        survives for external callers, but coordinator-owned code
        walks the class path.
        """
        return ProjectMemoryBank(
            self.project_dir,
            read_claude_fallback=self.read_claude_md,
        ).load_index()

    def load_user(self) -> str:
        """User-level global rules.

        Reads the ``USER_RULES_*`` module-level names off
        :mod:`context` at call time — see class docstring for
        why they're NOT stored on the instance.
        """
        # Deferred import: reading the names off the module
        # namespace at call time is the whole point. A module-
        # top import here would freeze them at import time and
        # defeat test monkeypatches.
        from ember_code.core.utils import context as _facade

        return load_user_rules(
            self.readers.read_with_imports,
            self.readers.read_rules_dir_files,
            working_dir=self.working_dir,
            project_dir=self.project_dir,
            read_claude_rules=self.read_claude_md,
            user_rules_path=_facade.USER_RULES_PATH,
            user_rules_dir=_facade.USER_RULES_DIR,
            claude_user_rules_dir=_facade.CLAUDE_USER_RULES_DIR,
        )

    def load_project_root(self) -> str:
        """Project-root ``ember.md`` / ``CLAUDE.md`` (+ ``.local.md`` overrides)."""
        return load_project_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            read_claude_md=self.read_claude_md,
        )

    def load_project_dirs(self) -> str:
        """Committed shared rules at ``<project>/.ember/rules/`` + ``<project>/.claude/rules/``."""
        return load_project_rules_dirs(
            self.readers.read_rules_dir_files,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )

    def load_subdirectory(self) -> list[SubdirectoryRules]:
        """Rules files between working-dir and project root, shallowest first."""
        pairs = load_subdirectory_rules(
            self.readers.read_rules_dir,
            self.readers.rules_filenames,
            project_dir=self.project_dir,
            working_dir=self.working_dir,
            read_claude_md=self.read_claude_md,
        )
        return [
            SubdirectoryRules(rel_path=rel_path, content=content) for rel_path, content in pairs
        ]

    # ── Top-level aggregation ────────────────────────────────

    def load_all(self) -> RulesBundle:
        """Load every tier and assemble the ordered :class:`RulesBundle`.

        Iterates the six tiers polymorphically — no dispatch
        dict, no six-branch if-chain. Subdirectory rules are the
        one tier that can produce multiple sections (one per
        walked directory), handled after the flat-tier loop.
        """
        sections: list[RulesSection] = []
        for tier in self.TIERS:
            body = tier.load(self)
            if body:
                sections.append(RulesSection(heading=tier.heading, body=body))
        for subdir in self.load_subdirectory():
            sections.append(subdir.to_section())
        return RulesBundle(sections=sections)
