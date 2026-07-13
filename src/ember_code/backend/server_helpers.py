"""Pure helpers for :mod:`ember_code.backend.server`.

Extracted from the ~4500-LoC god-file so the ``BackendServer``
class has less noise around it. Every helper here is a
stateless utility — no BackendServer / Session state.

Contents:

* :func:`_is_within` — safe "path is under root" check.
* :func:`_guess_language` — filename ext → Prism-compatible
  language string.
* :func:`_scan_plugin_dir` — walk a plugin directory to build
  the bundled-contents inventory (skills, agents, hooks, MCP
  servers, tools, README). Shared between the "installed
  plugin details" and "marketplace preview" paths.
* :func:`_search_code_cache_put` + :data:`_SEARCH_CODE_CACHE_MAX`
  — LRU-ish bounded cache for the search_code RPC.
* :func:`_search_history` + :data:`_SEARCH_CHAT_SNIPPET_HALF_WIDTH`
  — substring scan over a chat-history list for the
  ``search_chat`` RPC.
* :func:`_split_assistant_content_for_restore` — split an
  assistant message into ``(role, text)`` segments so
  ``<think>`` blocks restore as thinking cards on session
  reload.
* :func:`_format_tool_args_for_restore` — one-line ``key=value``
  formatter matching the live tool-card args preview.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class PluginSkillInfo(BaseModel):
    """One skill entry in :attr:`PluginContents.skills`."""

    name: str
    description: str = ""


class PluginAgentInfo(BaseModel):
    """One agent entry in :attr:`PluginContents.agents`."""

    name: str
    description: str = ""


class PluginHookInfo(BaseModel):
    """One hook-event entry in :attr:`PluginContents.hooks`.
    ``count`` is the number of handlers registered for the
    event."""

    event: str
    count: int


class PluginMCPServerInfo(BaseModel):
    """One MCP-server entry in :attr:`PluginContents.mcp_servers`."""

    name: str
    transport: str
    command: str


class PluginToolInfo(BaseModel):
    """One custom-tool entry in :attr:`PluginContents.tools`."""

    name: str


class PluginContents(BaseModel):
    """Wire shape returned by :func:`_scan_plugin_dir` (and the
    :func:`ember_code.backend.server_plugin.preview_plugin`
    marketplace-preview path that unwraps a clone into the same
    shape).

    ``error`` is populated when the caller failed to construct
    the payload (plugin not found, git clone failed, etc.) — in
    that case the collection fields stay empty and the FE renders
    the error card."""

    name: str = ""
    root_path: str = ""
    skills: list[PluginSkillInfo] = []
    agents: list[PluginAgentInfo] = []
    hooks: list[PluginHookInfo] = []
    mcp_servers: list[PluginMCPServerInfo] = []
    tools: list[PluginToolInfo] = []
    readme: str = ""
    error: str = ""

_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}


def _is_within(child: Path, root: Path) -> bool:
    """True iff ``child`` (already resolved) sits under ``root``."""
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _guess_language(suffix: str) -> str:
    """Return the Prism-compatible language string for a file suffix."""
    return _LANG_BY_EXT.get(suffix.lower(), "")


def _scan_plugin_dir(root: Path, *, name: str) -> PluginContents:
    """Walk *root* and pull out the bundled-contents inventory: skills,
    agents, hooks, MCP servers, custom tools, plus a README excerpt.

    Shared between :meth:`BackendServer.get_plugin_contents`
    (installed plugins) and :meth:`BackendServer.preview_plugin`
    (uninstalled catalog entries, scanned from a shallow clone).
    Pure on the filesystem — no plugin loader / session state
    needed.
    """
    result = PluginContents(name=name, root_path=str(root))

    def _frontmatter_field(md_text: str, field: str) -> str:
        if not md_text.startswith("---"):
            return ""
        end = md_text.find("\n---", 4)
        if end <= 0:
            return ""
        for line in md_text[4:end].splitlines():
            if line.lower().startswith(f"{field}:"):
                return line.split(":", 1)[1].strip().strip('"')
        return ""

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for sd in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = sd / "SKILL.md"
            desc = ""
            if skill_md.is_file():
                with contextlib.suppress(OSError):
                    desc = _frontmatter_field(skill_md.read_text(errors="replace"), "description")
            result.skills.append(PluginSkillInfo(name=sd.name, description=desc))

    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for af in sorted(agents_dir.glob("*.md")):
            desc = ""
            with contextlib.suppress(OSError):
                desc = _frontmatter_field(af.read_text(errors="replace"), "description")
            result.agents.append(PluginAgentInfo(name=af.stem, description=desc))

    hooks_json = root / "hooks" / "hooks.json"
    if hooks_json.is_file():
        try:
            data = json.loads(hooks_json.read_text())
            for event, handlers in (data.get("hooks") or {}).items():
                if isinstance(handlers, list):
                    result.hooks.append(PluginHookInfo(event=event, count=len(handlers)))
        except (OSError, json.JSONDecodeError):
            pass

    for mcp_name in (".mcp.json", "mcp.json"):
        mcp_path = root / mcp_name
        if mcp_path.is_file():
            try:
                data = json.loads(mcp_path.read_text())
                for srv_name, cfg in (data.get("mcpServers") or {}).items():
                    result.mcp_servers.append(
                        PluginMCPServerInfo(
                            name=srv_name,
                            transport=cfg.get("type", "stdio"),
                            command=cfg.get("command") or cfg.get("url") or "",
                        )
                    )
            except (OSError, json.JSONDecodeError):
                pass
            break

    tools_dir = root / "tools"
    if tools_dir.is_dir():
        for tf in sorted(tools_dir.glob("*.py")):
            if tf.name.startswith("_"):
                continue
            result.tools.append(PluginToolInfo(name=tf.stem))

    # README — capped generously so even long docs render in full but
    # a runaway file can't blow up the wire. Plugin READMEs in the
    # wild top out well under this.
    README_CAP = 200_000
    for readme_name in ("README.md", "Readme.md", "readme.md"):
        rp = root / readme_name
        if rp.is_file():
            try:
                text = rp.read_text(errors="replace")
                if len(text) > README_CAP:
                    result.readme = (
                        text[:README_CAP]
                        + "\n\n_…README truncated — open the source repo for the rest._"
                    )
                else:
                    result.readme = text
            except OSError:
                pass
            break

    return result


# Cap on the in-process search_code cache. Keyed by (project_root,
# max_results, snippet) — a few dozen entries is plenty for normal
# usage and the entries themselves are small JSON-shaped dicts. Old
# entries fall off in insertion order (Python dicts preserve
# insertion order, so a pop + re-set bumps to MRU).
_SEARCH_CODE_CACHE_MAX = 64


def _search_code_cache_put(cache: dict, key: str, value: Any) -> None:
    """LRU-ish insert with a size cap."""
    cache[key] = value
    while len(cache) > _SEARCH_CODE_CACHE_MAX:
        cache.pop(next(iter(cache)))


# Width on either side of a match for the snippet we ship to the
# FE. Generous enough for the user to see context but tight enough
# to keep the search-results dropdown skimmable.
_SEARCH_CHAT_SNIPPET_HALF_WIDTH = 80


def _search_history(history: list[dict], needle: str, limit: int) -> list[dict]:
    """Substring scan over a get_chat_history result.

    Extracted from BackendServer so it can be unit-tested without
    spinning up an Agno session.
    """
    needle_lower = needle.lower()
    needle_len = len(needle)
    if needle_len == 0:
        # Defense in depth — caller already strips, but ``find("")``
        # returns 0 for every string and would emit a match for every
        # turn. Empty query → no matches.
        return []
    matches: list[dict] = []
    for idx, turn in enumerate(history):
        content = turn.get("content")
        if not isinstance(content, str) or not content:
            continue
        pos = content.lower().find(needle_lower)
        if pos < 0:
            continue
        start = max(0, pos - _SEARCH_CHAT_SNIPPET_HALF_WIDTH)
        end = min(len(content), pos + needle_len + _SEARCH_CHAT_SNIPPET_HALF_WIDTH)
        snippet = content[start:end]
        leading_ellipsis = "…" if start > 0 else ""
        trailing_ellipsis = "…" if end < len(content) else ""
        snippet = f"{leading_ellipsis}{snippet}{trailing_ellipsis}"
        # Position of the match within the snippet string (not the
        # original content) — keeps the FE highlight logic trivial.
        match_start = (pos - start) + len(leading_ellipsis)
        matches.append(
            {
                "history_index": idx,
                "role": str(turn.get("role") or ""),
                "run_id": str(turn.get("run_id") or ""),
                "snippet": snippet,
                "match_start": match_start,
                "match_end": match_start + needle_len,
                # Epoch seconds (Agno-issued) — the FE formats it into
                # a relative "2h ago" / locale time string per row.
                "created_at": int(turn.get("created_at") or 0),
            }
        )
        if len(matches) >= limit:
            break
    return matches


# Inline ``<think>...</think>`` block — many models emit reasoning in
# the assistant content with these tags instead of Agno's
# ``reasoning_content`` field. The trailing ``|$`` allows a final
# unclosed block (cancelled run) to be captured up to end-of-content.
_THINK_BLOCK_RE = re.compile(r"<think>([\s\S]*?)(?:</think>|$)")


def _split_assistant_content_for_restore(content: str) -> list[tuple[str, str]]:
    """Split an assistant message's content into interleaved
    ``(role, text)`` segments, where ``role`` is ``"thinking"`` for
    ``<think>...</think>`` blocks and ``"assistant"`` for everything
    else. Preserves order so the rebuilt chat reads the same as the
    live stream.

    Returns ``[]`` when content has only whitespace / empty think
    blocks (degenerate runs); the caller should emit nothing then.
    """
    if "<think>" not in content:
        stripped = content.strip()
        return [("assistant", stripped)] if stripped else []
    parts: list[tuple[str, str]] = []
    cursor = 0
    for match in _THINK_BLOCK_RE.finditer(content):
        before = content[cursor : match.start()].strip()
        if before:
            parts.append(("assistant", before))
        thinking = match.group(1).strip()
        if thinking:
            parts.append(("thinking", thinking))
        cursor = match.end()
    trailing = content[cursor:].strip()
    if trailing:
        parts.append(("assistant", trailing))
    return parts


def _format_tool_args_for_restore(args: Any) -> str:
    """One-line argument summary for restored tool cards.

    Matches the live ``args_summary`` shape: ``key=value`` pairs
    joined by spaces, with long values truncated. Strings are shown
    raw (not JSON-quoted) so a shell ``command="ls -la"`` reads
    like a command, not like JSON.
    """
    if isinstance(args, dict):
        parts: list[str] = []
        for k, v in args.items():
            if isinstance(v, str):
                v_str = v if len(v) <= 80 else v[:77] + "..."
            elif isinstance(v, (int, float, bool)) or v is None:
                v_str = str(v)
            else:
                try:
                    v_str = json.dumps(v, separators=(",", ":"))
                except Exception:
                    v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
            parts.append(f"{k}={v_str}")
        return " ".join(parts)
    if isinstance(args, list):
        try:
            return json.dumps(args, separators=(",", ":"))
        except Exception:
            return str(args)
    return str(args)
