"""Output-style discovery + parsing.

Mirrors the markdown-commands shape: one ``.md`` file per style,
YAML frontmatter for ``name`` / ``description``, body becomes the
text appended to the agent's instructions when the style is
active. Last-write-wins precedence so a project style overrides a
user style of the same name, and plugin-shipped styles override
user but not project (matches markdown commands).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass(frozen=True)
class OutputStyle:
    """One discovered output style. ``body`` is the markdown text
    that gets appended to the agent's ``instructions`` list when
    this style is active. ``description`` is the one-line summary
    surfaced in slash-command listings and the panel."""

    name: str
    path: Path
    description: str
    body: str


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Pull a YAML frontmatter block off ``content``. Returns
    ``({}, content)`` on missing / malformed frontmatter so a bad
    file never sinks discovery."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    raw, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.debug("Output-style frontmatter parse failed: %s", exc)
        return {}, body
    if not isinstance(meta, dict):
        return {}, body
    return meta, body


def _load_style_file(path: Path) -> OutputStyle | None:
    if path.name.startswith("."):
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Output-style read %s failed: %s", path, exc)
        return None
    meta, body = _parse_frontmatter(content)
    # ``name`` defaults to the filename stem (matches the
    # convention; explicit frontmatter ``name:`` lets a style
    # advertise itself under a different label).
    name = str(meta.get("name", "")).strip() or path.stem
    description = str(meta.get("description", "")).strip()
    return OutputStyle(
        name=name,
        path=path.resolve(),
        description=description,
        body=body.strip(),
    )


def _style_dirs(
    project_dir: Path,
    plugin_roots: list[tuple[Path, str]] | None,
    read_claude: bool,
) -> list[Path]:
    """Roots to scan, in load order (later overrides earlier)."""
    home = Path.home()
    roots: list[Path] = []
    if read_claude:
        roots.append(home / ".claude" / "output-styles")
    roots.append(home / ".ember" / "output-styles")
    if read_claude:
        roots.append(project_dir / ".claude" / "output-styles")
    roots.append(project_dir / ".ember" / "output-styles")
    for plugin_root, _ in plugin_roots or []:
        roots.append(plugin_root / "output-styles")
    return roots


def discover_output_styles(
    project_dir: Path,
    plugin_roots: list[tuple[Path, str]] | None = None,
    read_claude: bool = True,
) -> dict[str, OutputStyle]:
    """Walk every configured root and return ``name → style``.

    Later roots override earlier ones on name collisions, so a
    project-level override of a bundled style works the same way
    as it does for slash commands / skills.
    """
    out: dict[str, OutputStyle] = {}
    for root in _style_dirs(project_dir, plugin_roots, read_claude):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            style = _load_style_file(path)
            if style is not None:
                out[style.name] = style
    return out
