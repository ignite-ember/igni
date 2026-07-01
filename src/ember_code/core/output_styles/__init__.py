"""Output styles — Claude Code parity (row 52).

A user-switchable system-prompt extension that adjusts how the
agent communicates (terse, explanatory, mentor, etc.) without
changing what tools it can use. Files live as markdown with
YAML frontmatter in:

* ``<project>/.ember/output-styles/<name>.md``
* ``~/.ember/output-styles/<name>.md``
* ``<project>/.claude/output-styles/<name>.md`` (cross-tool, gated)
* ``~/.claude/output-styles/<name>.md`` (cross-tool, gated)
* Plugin-bundled ``<plugin>/output-styles/<name>.md``

Discovery precedence (last write wins): user-claude < user-ember <
project-claude < project-ember < plugin. Identical model to
markdown-commands / skills.
"""

from ember_code.core.output_styles.loader import (
    OutputStyle,
    discover_output_styles,
)

__all__ = ["OutputStyle", "discover_output_styles"]
