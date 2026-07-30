"""User-level global rules — ``~/.ember/rules.md`` legacy file +
``~/.ember/rules/*.md`` directory form + ``~/.claude/rules/*.md``
cross-tool form.

Extracted from :mod:`ember_code.core.utils.context` per
CODE_STANDARDS.md Pattern 8. Same dependency-injection design as
:mod:`context_managed`: this module holds the paths + the loader
function; the shared rules-reading helpers (``_read_with_imports``,
``_read_rules_dir_files``) are passed in by the caller so this
module stays a leaf in the import graph and CODE_STANDARDS Rule 2
(no inline imports) holds.

## Sources loaded (in order)

1. ``~/.ember/rules.md`` — legacy single-file form. Kept for users
   who set that up before the directory form was added.
2. ``~/.ember/rules/*.md`` — ember-native directory form. One file
   per topic (e.g. ``coding.md``, ``testing.md``). Files with a
   ``paths:`` frontmatter contribute only when the session's
   ``working_dir`` matches one of the globs.
3. ``~/.claude/rules/*.md`` — cross-tool form, gated on
   ``read_claude_rules``. Lets a user who has Claude Code rules
   set up already have them apply to ember sessions too without
   duplicating.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Path constants — public so tests + downstream can monkeypatch them
# in a hermetic sandbox (see ``test_context.py::_isolate_user_rules``).
USER_RULES_PATH = Path.home() / ".ember" / "rules.md"
USER_RULES_DIR = Path.home() / ".ember" / "rules"
CLAUDE_USER_RULES_DIR = Path.home() / ".claude" / "rules"


def load_user_rules(
    read_with_imports: Callable[..., str],
    read_rules_dir_files: Callable[..., str],
    working_dir: Path | None = None,
    project_dir: Path | None = None,
    read_claude_rules: bool = True,
    user_rules_path: Path = USER_RULES_PATH,
    user_rules_dir: Path = USER_RULES_DIR,
    claude_user_rules_dir: Path = CLAUDE_USER_RULES_DIR,
) -> str:
    """Load user-level global rules from all configured sources.

    Sources, concatenated in order:

    1. ``~/.ember/rules.md`` (legacy single-file form)
    2. ``~/.ember/rules/*.md`` (directory form)
    3. ``~/.claude/rules/*.md`` (cross-tool, when ``read_claude_rules``)

    Files with ``paths:`` frontmatter contribute only when
    ``working_dir`` matches one of the globs.

    Path arguments default to the module-level constants but tests
    (or a caller) may override them — this preserves the old
    ``monkeypatch.setattr(context, "USER_RULES_PATH", ...)`` idiom
    at the wrapper layer: ``context.py`` passes its own module-level
    names, which tests patch. Without the pass-through the extraction
    would silently break every hermetic-sandbox rules test.

    ``read_with_imports`` and ``read_rules_dir_files`` are injected
    — they live in ``context.py`` and are shared across all rule
    sources. Passing them by argument keeps this module a leaf in
    the import graph (context.py imports us; we don't import back).
    """
    sections: list[str] = []
    legacy = read_with_imports(user_rules_path, allowed_root=user_rules_path.parent)
    if legacy:
        sections.append(legacy)
    ember_dir = read_rules_dir_files(user_rules_dir, working_dir, project_dir)
    if ember_dir:
        sections.append(ember_dir)
    if read_claude_rules:
        claude_dir = read_rules_dir_files(claude_user_rules_dir, working_dir, project_dir)
        if claude_dir:
            sections.append(claude_dir)
    return "\n\n".join(sections)
