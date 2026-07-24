"""Tool invocation value object.

Owns the three rule-string builders that used to be module-level
free functions (``build_rule`` / ``build_pattern_rule`` /
``_format_args_for_rule``). Each is now a real method on
:class:`ToolInvocation` — a small value object holding the tool
name plus its typed args.

Rule 6 offender killed: those three free functions took a
``tool_name: str`` first arg and a ``tool_args: dict`` second arg,
then reached into the dict via key lookups. Bundling both into a
value object turns each helper into a method on the state that
carries the data it needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from ember_code.core.config.tool_permissions.schemas import ToolInvocationArgs


class ToolInvocation(BaseModel):
    """A concrete tool call (tool name + typed args).

    Used by both the pre-approval side (permission checking) and the
    post-approval side (persisting an "always allow" rule the user
    picked in the HITL dialog). All three flavours of rule-string
    emission (:meth:`exact_rule`, :meth:`pattern_rule`,
    :meth:`short_args_str`) live here so no caller needs to poke
    inside the raw args dict.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    args: ToolInvocationArgs

    @classmethod
    def from_raw(cls, tool_name: str, tool_args: dict[str, Any] | None) -> ToolInvocation:
        """Adapter for callers still on raw dicts (backend HITL path,
        legacy tests). New call sites should construct a
        :class:`ToolInvocation` directly."""
        return cls(tool_name=tool_name, args=ToolInvocationArgs.from_dict(tool_args))

    # ── Rule-string emission ─────────────────────────────────────

    def exact_rule(self) -> str:
        """Build an exact-invocation rule string.

        Was ``build_rule(tool_name, tool_args)``. Emits e.g.
        ``Bash(python3 -m http.server 8000)`` for a specific shell
        call, or falls back to the bare tool name when no
        distinguishable args exist.
        """
        args_str = self.short_args_str()
        if args_str:
            return f"{self.tool_name}({args_str})"
        return self.tool_name

    def pattern_rule(self) -> str:
        """Build a broadening "similar" rule string.

        Was ``build_pattern_rule(tool_name, tool_args)``. Emits raw
        fnmatch patterns (``python3 *``, ``src/*``) rather than
        ``ToolPermissions`` legacy prefix syntax (``python3:*``,
        ``path:src/*``). The raw form is what both matchers accept:
        :meth:`PermissionRule.matches` fnmatches the primary arg,
        and :class:`GlobPattern` fnmatches the primary args string.
        Before this consolidation the two matchers disagreed and
        "Allow similar" clicked in the web dialog persisted a rule
        that only the TUI's check-permission RPC could see.

        Falls back to bare ``tool_name`` when no pattern can be
        derived.
        """
        args = self.args

        # Shell tools with an ``args`` list: pattern on the leading
        # binary name.
        if args.args:
            return f"{self.tool_name}({args.args[0]} *)"

        # Shell tools with a ``command`` string: same shape,
        # extracted from the first whitespace-split token.
        if args.command:
            first = str(args.command).strip().split()
            if first:
                return f"{self.tool_name}({first[0]} *)"

        # File tools: whitelist the parent directory as a glob.
        path_value = args.path or args.file_path
        if path_value:
            parent = str(Path(str(path_value)).parent)
            if parent and parent != ".":
                return f"{self.tool_name}({parent}/*)"

        # Web tools: whitelist the domain.
        if args.url:
            domain = urlparse(str(args.url)).netloc
            if domain:
                return f"{self.tool_name}(domain:{domain})"

        return self.tool_name

    def short_args_str(self) -> str:
        """Short args representation embedded in :meth:`exact_rule`.

        Was ``_format_args_for_rule(args)``. Preserves the historical
        wire shape (``Bash(python3 -m http.server 8000)`` etc.) so
        rules persisted by the previous procedural implementation
        continue to round-trip byte-for-byte through this method.
        """
        args = self.args
        if args.args:
            return " ".join(str(a) for a in args.args)
        if args.command:
            return str(args.command)
        for value in (args.path, args.file_path, args.url, args.query):
            if value:
                return str(value)
        # Fallback: whatever's left in the args payload, truncated.
        payload = args.as_dict()
        if not payload:
            return ""
        return str(payload)[:100]
