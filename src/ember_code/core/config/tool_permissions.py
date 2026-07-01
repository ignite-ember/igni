"""Tool permission settings — Claude Code-style allow/ask/deny with argument rules.

Reads from (highest priority last):
1. ~/.ember/settings.json (user global defaults)
2. ~/.ember/settings.local.json (user local overrides, runtime saves)
3. .ember/settings.json (project overrides, committed)
4. .ember/settings.local.json (project local overrides)

Format:
{
  "permissions": {
    "allow": [
      "Read",
      "Grep",
      "Bash(git status)",
      "Bash(git diff:*)",
      "WebFetch(domain:github.com)"
    ],
    "ask": ["Bash", "Write", "Edit"],
    "deny": ["WebSearch"]
  }
}

Rules:
- "ToolName"              — matches all calls to that tool
- "ToolName(exact args)"  — matches specific arguments
- "ToolName(prefix:*)"    — matches arguments starting with prefix
- "ToolName(key:value)"   — matches a specific key in the tool args dict
"""

import contextlib
import fnmatch
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default permission levels for each tool (bare tool name)
_DEFAULTS: dict[str, str] = {
    "Read": "allow",
    "Glob": "allow",
    "Grep": "allow",
    "LS": "allow",
    "Write": "ask",
    "Edit": "ask",
    "Bash": "ask",
    "BashOutput": "ask",
    "Python": "ask",
    "WebSearch": "allow",
    "WebFetch": "allow",
    "NotebookEdit": "ask",
}

# Maps Agno function names to our tool names
FUNC_TO_TOOL: dict[str, str] = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "save_file": "Write",
    "list_files": "LS",
    "edit_file": "Edit",
    "replace_in_file": "Edit",
    "grep_search": "Grep",
    "glob_files": "Glob",
    "web_fetch": "WebFetch",
    "duckduckgo_search": "WebSearch",
    "duckduckgo_news": "WebSearch",
    "run_python_code": "Python",
    "notebook_read": "NotebookEdit",
    "notebook_read_cell": "NotebookEdit",
    "notebook_edit_cell": "NotebookEdit",
    "notebook_add_cell": "NotebookEdit",
    "notebook_remove_cell": "NotebookEdit",
}

_RULE_RE = re.compile(r"^(\w+)(?:\((.+)\))?$")


def _parse_rule(rule: str) -> tuple[str, str | None]:
    """Parse a rule like 'Bash(git:*)' into (tool_name, arg_pattern)."""
    m = _RULE_RE.match(rule.strip())
    if not m:
        return rule.strip(), None
    return m.group(1), m.group(2)


def _args_to_str(tool_args: dict[str, Any] | None) -> str:
    """Convert tool args dict to a matchable string."""
    if not tool_args:
        return ""
    # For shell commands: join the args list
    if "args" in tool_args and isinstance(tool_args["args"], list):
        return " ".join(str(a) for a in tool_args["args"])
    # For file operations: use the path/file_path
    for key in ("path", "file_path", "file_name", "url", "query"):
        if key in tool_args:
            return str(tool_args[key])
    # Fallback: serialize all values
    return " ".join(str(v) for v in tool_args.values())


def _extract_domain(url: str) -> str:
    """Extract domain from a URL."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc
    except Exception as exc:
        logger.debug("Failed to extract domain from URL: %s", exc)
        return ""


def _match_rule_args(pattern: str, tool_name: str, tool_args: dict[str, Any] | None) -> bool:
    """Check if tool args match a rule's argument pattern.

    Patterns:
    - "git status"         → exact match against args string
    - "git:*"              → prefix wildcard match
    - "domain:github.com"  → key:value match against extracted properties
    - "path:src/*"         → glob match against file path
    """
    args_str = _args_to_str(tool_args)

    # key:value patterns
    if ":" in pattern:
        key, value = pattern.split(":", 1)

        if key == "domain" and tool_args:
            # Extract domain from URL args
            url = tool_args.get("url", "") or tool_args.get("query", "")
            domain = _extract_domain(str(url))
            return fnmatch.fnmatch(domain, value)

        if key == "path" and tool_args:
            path = (
                tool_args.get("path", "")
                or tool_args.get("file_path", "")
                or tool_args.get("file_name", "")
            )
            return fnmatch.fnmatch(str(path), value)

        # Generic: treat as prefix:glob against the full args string
        return fnmatch.fnmatch(args_str, f"{key} {value}" if " " not in key else pattern)

    # Direct match or glob against args string
    return fnmatch.fnmatch(args_str, pattern)


class ToolPermissions:
    """Resolves per-tool permission levels from settings files.

    Supports both bare tool rules ("Bash") and argument-specific rules
    ("Bash(git status)", "WebFetch(domain:github.com)").

    Resolution order for a specific call:
    1. Check argument-specific rules (most specific wins)
    2. Fall back to bare tool-level rule
    3. Fall back to default ("ask")
    """

    # Maps Settings.permissions fields to tool names
    _SETTINGS_TO_TOOL: dict[str, list[str]] = {
        "file_read": ["Read", "Glob", "Grep", "LS"],
        "file_write": ["Write", "Edit"],
        "shell_execute": ["Bash", "BashOutput", "Python"],
        "web_search": ["WebSearch"],
        "web_fetch": ["WebFetch"],
    }

    def __init__(self, project_dir: Path | None = None, settings_permissions: Any = None):
        self._project_dir = project_dir or Path.cwd()
        # Bare tool-level permissions
        self._tool_levels: dict[str, str] = dict(_DEFAULTS)
        # Argument-specific rules: list of (tool_name, arg_pattern, level)
        self._rules: list[tuple[str, str, str]] = []
        self._load()
        # Apply Settings.permissions overrides (from CLI flags / config YAML)
        # These take priority over settings.json files
        if settings_permissions is not None:
            for field, tools in self._SETTINGS_TO_TOOL.items():
                level = getattr(settings_permissions, field, None)
                if level is not None:
                    for tool in tools:
                        if _DEFAULTS.get(tool) != level:
                            self._tool_levels[tool] = level

    def _load(self) -> None:
        """Load settings files in priority order (last wins).

        Hierarchy:
        1. ~/.ember/settings.json (user global defaults)
        2. ~/.ember/settings.local.json (user local overrides)
        3. .ember/settings.json (project overrides, committed)
        4. .ember/settings.local.json (project local overrides, gitignored)
        """
        home_ember = Path.home() / ".ember"
        paths = [
            home_ember / "settings.json",
            home_ember / "settings.local.json",
            self._project_dir / ".ember" / "settings.json",
            self._project_dir / ".ember" / "settings.local.json",
        ]
        for path in paths:
            self._apply_file(path)

    def _apply_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            perms = data.get("permissions", {})
            for level in ("allow", "ask", "deny"):
                for rule in perms.get(level, []):
                    tool_name, arg_pattern = _parse_rule(rule)
                    if arg_pattern:
                        self._rules.append((tool_name, arg_pattern, level))
                    else:
                        self._tool_levels[tool_name] = level
        except Exception as e:
            logger.warning("Failed to load %s: %s", path, e)

    def check(
        self, tool_name: str, func_name: str | None = None, tool_args: dict[str, Any] | None = None
    ) -> str:
        """Check permission for a specific tool call.

        Args:
            tool_name: Our tool name (e.g. "Bash", "Write")
            func_name: Agno function name (e.g. "run_shell_command")
            tool_args: The actual arguments being passed

        Returns:
            "allow", "ask", or "deny"
        """
        # Resolve tool name from function name if needed
        if not tool_name and func_name:
            tool_name = FUNC_TO_TOOL.get(func_name, func_name)

        # Check argument-specific rules first (last matching rule wins)
        matched_level = None
        for rule_tool, arg_pattern, level in self._rules:
            if rule_tool == tool_name and _match_rule_args(arg_pattern, tool_name, tool_args):
                matched_level = level

        if matched_level is not None:
            return matched_level

        # Fall back to bare tool-level
        return self._tool_levels.get(tool_name, "ask")

    # Convenience methods for bare tool-level checks (used at registry time)
    def get_level(self, tool_name: str) -> str:
        return self._tool_levels.get(tool_name, "ask")

    def is_denied(self, tool_name: str) -> bool:
        return self.get_level(tool_name) == "deny"

    def needs_confirmation(self, tool_name: str) -> bool:
        return self.get_level(tool_name) == "ask"

    def has_arg_rules(self, tool_name: str) -> bool:
        """Check if there are argument-specific rules for this tool."""
        return any(t == tool_name for t, _, _ in self._rules)

    def save_rule(self, rule: str, level: str) -> None:
        """Persist a permission rule to .ember/settings.local.json (project-local).

        Falls back to ~/.ember/settings.local.json if no project dir.

        Args:
            rule: e.g. "Bash", "Bash(git status)", "WebFetch(domain:github.com)"
            level: "allow", "ask", or "deny"
        """
        if self._project_dir:
            path = self._project_dir / ".ember" / "settings.local.json"
        else:
            path = Path.home() / ".ember" / "settings.local.json"
        data: dict[str, Any] = {}
        if path.exists():
            with contextlib.suppress(Exception):
                data = json.loads(path.read_text())

        perms = data.setdefault("permissions", {})
        # Remove from other lists if exact match exists
        for key in ("allow", "ask", "deny"):
            lst = perms.get(key, [])
            if rule in lst:
                lst.remove(rule)

        # Add to the right list
        perms.setdefault(level, []).append(rule)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")

        # Update in-memory
        tool_name, arg_pattern = _parse_rule(rule)
        if arg_pattern:
            self._rules.append((tool_name, arg_pattern, level))
        else:
            self._tool_levels[tool_name] = level


# ── Rule-string builders (shared by TUI + web HITL flows) ──────────
#
# Live outside ``ToolPermissions`` so callers who only need to
# compute a rule string (BE's ``resolve_hitl_batch``, the TUI's
# ``hitl_handler``) don't have to instantiate a permissions
# object. The TUI shipped an identical private copy — this is
# the canonical home; new call sites should import from here.


def build_rule(tool_name: str, tool_args: dict) -> str:
    """Build a specific-argument rule string, e.g.
    ``run_shell_command(python3 -m http.server 8000)``. Used when
    the user picks "Always allow" — matches this exact invocation
    only. Falls back to bare ``tool_name`` when no meaningful args
    can be extracted."""
    args_str = _format_args_for_rule(tool_args)
    if args_str:
        return f"{tool_name}({args_str})"
    return tool_name


def build_pattern_rule(tool_name: str, tool_args: dict) -> str:
    """Build a broader "similar" rule from a tool call — matches
    the same command family / directory / domain, not just this
    exact invocation. Used when the user picks "Allow similar":
    e.g. one ``python3 -m http.server 8000`` → whitelist all
    ``python3 *`` calls; one ``file_read(src/a.py)`` → whitelist
    ``src/*``; one ``web_fetch(https://x.com/a)`` → whitelist
    ``domain:x.com``.

    Emits raw fnmatch patterns (``python3 *``, ``src/*``) rather
    than ``ToolPermissions``-legacy prefix syntax (``python3:*``,
    ``path:src/*``). The raw form matches both:

    * ``PermissionEvaluator.matches`` — used in the web/HITL
      pre-check path via ``_generate_hitl_requirements`` — which
      does a straight fnmatch on the primary arg, so
      ``python3:*`` would never match ``python3 -m http.server``
      (no literal colon).
    * ``ToolPermissions._match_rule_args`` — used in the TUI
      pre-dialog RPC path — which handles a bare pattern too
      via its generic fnmatch fallback.

    Before this change the two matchers disagreed and "Allow
    similar" clicked in the web dialog persisted a rule that
    only the TUI's check-permission RPC could see — the web
    session's own evaluator kept re-prompting. Falls back to
    bare ``tool_name`` when no pattern can be derived.
    """
    from pathlib import Path

    if "args" in tool_args and isinstance(tool_args["args"], list):
        cmd = tool_args["args"]
        if cmd:
            return f"{tool_name}({cmd[0]} *)"
    if "command" in tool_args:
        # ``run_shell_command(command="python3 -m http.server 8000")``
        # → pattern on the leading token.
        first = str(tool_args["command"]).strip().split()
        if first:
            return f"{tool_name}({first[0]} *)"
    for key in ("path", "file_path"):
        if key in tool_args:
            parent = str(Path(str(tool_args[key])).parent)
            if parent and parent != ".":
                return f"{tool_name}({parent}/*)"
    if "url" in tool_args:
        from urllib.parse import urlparse

        domain = urlparse(str(tool_args["url"])).netloc
        if domain:
            return f"{tool_name}(domain:{domain})"
    return tool_name


def _format_args_for_rule(args: dict) -> str:
    """Short args representation used inside a specific-rule
    string. Same shape the TUI shipped so existing rules
    round-trip identically."""
    if "args" in args and isinstance(args["args"], list):
        return " ".join(str(a) for a in args["args"])
    if "command" in args:
        return str(args["command"])
    for key in ("path", "file_path", "url", "query"):
        if key in args:
            return str(args[key])
    return str(args)[:100]
