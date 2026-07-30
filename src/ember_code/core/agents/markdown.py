"""Parse a single agent ``.md`` file into an :class:`AgentDefinition`.

Encapsulates two operations that used to be free functions:
:func:`parse_agent_file` and :func:`_raw_frontmatter_keys`. Both
reads shared the same file-open + YAML parse; the class caches
the parsed frontmatter after the first access so the plugin-
restriction path stops paying two file reads per agent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import yaml

from ember_code.core.agents.schemas import AgentDefinition


class AgentMarkdownFile:
    """A ``.md`` file on disk that carries YAML frontmatter +
    an agent's system prompt body.

    Construction is cheap — no file IO happens until :meth:`parse`
    or :meth:`raw_frontmatter_keys` is called. Both methods share
    the same parsed frontmatter (cached on the instance) so a
    single file is never read twice.
    """

    FRONTMATTER_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self._frontmatter: dict | None = None
        self._body: str | None = None
        self._parse_ok: bool | None = None

    def _ensure_parsed(self) -> None:
        """Read + split + YAML-parse. Sets ``_parse_ok`` to True on
        success, False on any failure. Safe to call repeatedly —
        subsequent calls are a noop."""
        if self._parse_ok is not None:
            return
        try:
            content = self.path.read_text()
        except (OSError, UnicodeDecodeError):
            self._parse_ok = False
            return
        match = self.FRONTMATTER_RE.match(content)
        if not match:
            self._parse_ok = False
            return
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            self._parse_ok = False
            return
        if not isinstance(fm, dict):
            self._parse_ok = False
            return
        self._frontmatter = fm
        self._body = match.group(2).strip()
        self._parse_ok = True

    def raw_frontmatter_keys(self) -> set[str]:
        """Top-level YAML frontmatter keys, or empty on any failure.

        Used by the plugin-restriction filter to warn about
        restricted keys that :meth:`parse` itself silently drops."""
        self._ensure_parsed()
        if not self._parse_ok or self._frontmatter is None:
            return set()
        return set(self._frontmatter.keys())

    def parse(self) -> AgentDefinition:
        """Parse the file into an :class:`AgentDefinition`.

        Raises ``ValueError`` on missing / malformed frontmatter or
        missing ``name`` / ``description`` fields — the caller is
        expected to catch and route to a :class:`LoadError`.
        """
        self._ensure_parsed()
        if not self._parse_ok:
            # Distinguish "no frontmatter at all" vs "YAML broken":
            # re-read once, don't cache, and give a specific message.
            try:
                content = self.path.read_text()
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"Cannot read {self.path}: {exc}") from exc
            if not self.FRONTMATTER_RE.match(content):
                raise ValueError(f"No YAML frontmatter found in {self.path}")
            raise ValueError(f"Invalid YAML frontmatter in {self.path}")

        assert self._frontmatter is not None  # narrowed by _parse_ok
        assert self._body is not None
        fm = self._frontmatter
        body = self._body

        if "name" not in fm:
            raise ValueError(f"Agent definition missing 'name' in {self.path}")
        if "description" not in fm:
            raise ValueError(f"Agent definition missing 'description' in {self.path}")

        tools = _coerce_string_list(fm.get("tools", []))
        tags = _coerce_string_list(fm.get("tags", []))

        return AgentDefinition(
            name=fm["name"],
            description=fm["description"],
            tools=tools,
            model=fm.get("model"),
            color=fm.get("color"),
            reasoning=fm.get("reasoning", False),
            reasoning_min_steps=fm.get("reasoning_min_steps", 1),
            reasoning_max_steps=fm.get("reasoning_max_steps", 10),
            tags=tags,
            can_orchestrate=fm.get("can_orchestrate", True),
            mcp_servers=fm.get("mcp_servers", []) or [],
            max_turns=fm.get("max_turns"),
            temperature=fm.get("temperature"),
            max_tokens=fm.get("max_tokens"),
            system_prompt=body,
            source_path=self.path,
        )


def _coerce_string_list(raw: object) -> list[str]:
    """Convert a YAML frontmatter value into a list of stripped
    strings. Accepts a comma-separated string, a list, or anything
    else (returns ``[]``).

    Kept module-private (not a method) because it's a pure
    conversion utility with no state — a method here would be
    a Rule-6 offender.
    """
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


__all__ = ["AgentMarkdownFile"]
