"""Hierarchical rules discovery — Claude Code-style.

Scans the project tree at session start for ``ember.md`` /
``CLAUDE.md`` files in subdirectories, then surfaces them lazily as
the agent touches files in those areas. Once a rules file has been
shown to the agent in a session, it isn't re-shown on subsequent
tool calls touching the same directory — the model only needs the
context delivered once.

The root-level ``ember.md`` / ``CLAUDE.md`` are NOT part of this
index. Those are already loaded into the system prompt at session
init via ``load_project_context`` — they form the unconditional
baseline. This index handles only the subdirectory tier: rules that
should kick in *if* the agent works in that area, and stay quiet
otherwise.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ember_code.core.utils.context_frontmatter import parse_frontmatter
from ember_code.core.utils.context_imports import resolve_imports

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScopedRule:
    """A path-scoped rules file from ``<project>/.ember/rules/`` or
    ``<project>/.claude/rules/``.

    ``globs`` is the ``paths:`` list from the file's YAML
    frontmatter. ``body`` is the markdown after the frontmatter
    (already stripped, NOT yet ``@<path>``-import-resolved — that
    happens on demand in ``consume_path`` so the index stays cheap
    when most scoped rules never fire).
    """

    path: Path
    globs: tuple[str, ...]
    body: str
    # Carried for ``@<path>`` resolution context — imports inside a
    # scoped rule resolve against the project root, same as the
    # rest of the lazy-injection pipeline.
    _project_dir: Path = field(repr=False)


# Directories we never walk into when looking for rules files —
# either they're vendored / generated content (no human-authored
# instructions live there), or they're tool-config directories that
# follow their own conventions (``.ember`` / ``.claude`` host
# agents/skills/hooks, not freeform rules markdown).
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        ".env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".cache",
        ".idea",
        ".vscode",
        ".ember",
        ".claude",
    }
)


def _rules_filenames(read_claude_md: bool) -> tuple[str, ...]:
    # Keep in lockstep with ``context._rules_filenames`` — both
    # surfaces should see the same set of variants (incl. the
    # ``.local`` override siblings).
    if read_claude_md:
        return ("ember.md", "ember.local.md", "CLAUDE.md", "CLAUDE.local.md")
    return ("ember.md", "ember.local.md")


class RulesIndex:
    """Per-session map of subdirectory rules files.

    Construction walks the project tree once. ``consume_path`` is
    called by the tool-event hook on every successful tool call that
    references a file path; it returns the rules files that the
    agent hasn't seen yet for that path's ancestor chain.
    """

    def __init__(self, project_dir: Path, read_claude_md: bool = True) -> None:
        try:
            self.project_dir = project_dir.resolve()
        except OSError:
            self.project_dir = project_dir
        self._read_claude_md = read_claude_md
        self._filenames = _rules_filenames(read_claude_md)
        # ``{dir -> list of rules files in load order}``. Multiple
        # files per dir support the override pattern: a subdir that
        # ships both ``ember.md`` (committed) and ``ember.local.md``
        # (gitignored personal) surfaces both, with the local
        # variant appearing AFTER so its directives take precedence
        # in the agent's read order.
        self._index: dict[Path, list[Path]] = {}
        # Path-scoped rules from ``<project>/.ember/rules/*.md`` and
        # ``<project>/.claude/rules/*.md`` whose YAML frontmatter
        # has a ``paths:`` glob list. Loaded lazily by
        # ``consume_path`` only when the agent touches a file whose
        # path matches the rule's globs. Rules in the same
        # directories WITHOUT ``paths:`` frontmatter load eagerly
        # via ``load_project_rules_dirs`` instead — the two paths
        # don't overlap (the eager loader filters out scoped files
        # when ``working_dir`` is ``None``, which it is at session
        # start).
        self._scoped_rules: list[_ScopedRule] = []
        # Shared dedup set: both subdir ``ember.md`` files and
        # path-scoped rules surface at most once per session.
        self._shown: set[Path] = set()
        self._build()

    def _build(self) -> None:
        root = self.project_dir
        if not root.is_dir():
            return
        # ``rglob('*')`` would also descend into excluded dirs. Use a
        # manual walk so we can prune the excluded subtrees and
        # avoid scanning huge ``node_modules`` / ``target`` trees.
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except (PermissionError, OSError) as exc:
                logger.debug("RulesIndex skip %s (%s)", current, exc)
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name in _EXCLUDED_DIR_NAMES:
                        continue
                    if entry.is_symlink():
                        # Don't follow symlinked dirs — risks cycles
                        # and surprises (a symlink to /home/.config
                        # would suck in the world).
                        continue
                    stack.append(entry)
                    # Check for a rules file at this directory level
                    # while we're here. Collect ALL matching variants
                    # (not just the first) so .local overrides ride
                    # alongside their committed counterparts.
                    if entry != root:
                        matched: list[Path] = []
                        for name in self._filenames:
                            rules_file = entry / name
                            if rules_file.is_file():
                                matched.append(rules_file.resolve())
                        if matched:
                            self._index[entry.resolve()] = matched
        # Scan the project-level rules dirs explicitly. They live
        # inside ``.ember`` / ``.claude`` which the main walk above
        # excludes (those are plugin-config dirs).
        self._build_scoped_rules()
        logger.debug(
            "RulesIndex built: %d subdirectory rules files, %d path-scoped rules under %s",
            len(self._index),
            len(self._scoped_rules),
            root,
        )

    def _build_scoped_rules(self) -> None:
        """Scan ``<project>/.ember/rules/`` and (when enabled)
        ``<project>/.claude/rules/`` for files carrying a ``paths:``
        frontmatter, and register them in ``self._scoped_rules``.
        Unscoped files are ignored here — they're picked up by the
        eager ``load_project_rules_dirs`` loader instead."""
        candidates = [self.project_dir / ".ember" / "rules"]
        if self._read_claude_md:
            candidates.append(self.project_dir / ".claude" / "rules")
        for rules_dir in candidates:
            if not rules_dir.is_dir():
                continue
            for path in sorted(rules_dir.rglob("*.md")):
                if not path.is_file():
                    continue
                try:
                    content = path.read_text()
                except (OSError, UnicodeDecodeError) as exc:
                    logger.debug("RulesIndex scoped read %s failed: %s", path, exc)
                    continue
                globs, body = parse_frontmatter(content)
                if not globs:
                    # Unconditional — handled by the eager loader.
                    continue
                self._scoped_rules.append(
                    _ScopedRule(
                        path=path.resolve(),
                        globs=tuple(globs),
                        body=body.strip(),
                        _project_dir=self.project_dir,
                    )
                )

    def consume_path(self, path: Path | str) -> list[tuple[Path, str]]:
        """Return any *newly-encountered* rules files between
        ``path``'s parent directory and the project root.

        ``path`` can be a file or a directory; either way the walk
        starts at the nearest containing directory. Each rules file
        is returned at most once across the lifetime of this index
        — the second call asking about the same subtree will return
        an empty list for the previously-seen rules.

        The returned list is ordered shallowest-first so the agent
        reads the more general rules before the more specific ones.
        Paths outside ``project_dir`` produce an empty list.
        """
        # Both pools (subdir ``ember.md``-style + path-scoped
        # ``.ember/rules/*.md``) are checked; bail only when both
        # are empty.
        if not self._index and not self._scoped_rules:
            return []
        try:
            resolved = Path(path).resolve()
        except OSError:
            return []
        # If it's a file or doesn't exist, walk starts at its parent.
        # If it's a dir, walk starts at the dir itself.
        cursor = resolved if resolved.is_dir() else resolved.parent
        try:
            cursor.relative_to(self.project_dir)
        except ValueError:
            return []

        # Walk parent → … → project_dir (exclusive). project_dir's
        # rules are loaded into the system prompt already. We
        # collect per-dir results then flatten shallowest-first so
        # the within-dir order (base before ``.local``) is
        # preserved — a naive ``results.reverse()`` over a flat list
        # would invert the dir AND the per-dir ordering.
        per_dir: list[list[tuple[Path, str]]] = []
        while cursor != self.project_dir:
            dir_results: list[tuple[Path, str]] = []
            for rules_file in self._index.get(cursor, ()):
                if rules_file in self._shown:
                    continue
                try:
                    content = rules_file.read_text()
                except (OSError, UnicodeDecodeError) as exc:
                    logger.debug("RulesIndex read %s failed: %s", rules_file, exc)
                    continue
                # Inline ``@<path>.md`` references, scoped to
                # project_dir so an errant ``@/etc/passwd`` in a
                # rules file can't reach outside the repo.
                content = resolve_imports(content, rules_file, self.project_dir)
                self._shown.add(rules_file)
                dir_results.append((rules_file, content))
            if dir_results:
                per_dir.append(dir_results)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
        results: list[tuple[Path, str]] = []
        for chunk in reversed(per_dir):
            results.extend(chunk)
        # Path-scoped rules — fire when the touched path matches any
        # of the rule's ``paths:`` globs. Each rule shows at most
        # once per session, regardless of how many files in its
        # glob range the agent ends up touching.
        results.extend(self._match_scoped_rules(resolved))
        return results

    def _match_scoped_rules(self, touched: Path) -> list[tuple[Path, str]]:
        """Return path-scoped rules whose ``paths:`` globs match
        ``touched``, marking each as shown so subsequent calls
        won't re-emit them."""
        if not self._scoped_rules:
            return []
        # fnmatch globs work on both the project-relative path and
        # the absolute path — match against both so users can write
        # either ``clients/tauri/**`` or ``/abs/.../clients/tauri/**``.
        candidates: list[str] = [str(touched)]
        try:
            candidates.append(str(touched.relative_to(self.project_dir)))
        except ValueError:
            return []
        out: list[tuple[Path, str]] = []
        for rule in self._scoped_rules:
            if rule.path in self._shown:
                continue
            for pattern in rule.globs:
                if any(fnmatch.fnmatch(c, pattern) for c in candidates):
                    self._shown.add(rule.path)
                    body = resolve_imports(rule.body, rule.path, self.project_dir)
                    out.append((rule.path, body))
                    break
        return out

    def has_pending(self) -> bool:
        """``True`` if any indexed rules file hasn't been shown yet."""
        total = sum(len(files) for files in self._index.values())
        total += len(self._scoped_rules)
        return len(self._shown) < total
