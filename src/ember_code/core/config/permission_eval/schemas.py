"""Type definitions for the Claude Code-style permission evaluator.

Follows the sibling ``permissions/schemas.py`` / ``tool_permissions/schemas.py``
convention — every enum, value object and Pydantic model the evaluator
subsystem exposes lives here so the pipeline module owns only orchestration
logic, not type declarations.

Type map (old → new):

* ``PermissionMode(StrEnum)`` — unchanged.
* ``PermissionDecision(Enum)`` — kept as :class:`enum.Enum` (not ``StrEnum``)
  because callers compare via ``is`` semantics.
* ``PermissionOutcome`` — NEW: bundles ``(decision, reason, source)`` so
  ``explain_deny`` reads the reason produced by ``evaluate_typed`` instead
  of re-scanning the deny list (kills the two-scan drift risk).
* ``PermissionRule`` — was a frozen ``@dataclass`` with ``.parse`` /
  ``.parse_many`` classmethods and ``.matches``. Kept as a Pydantic
  ``BaseModel`` here because tests construct it directly with
  ``PermissionRule(tool=..., pattern=...)`` and compare via
  ``rule.tool`` / ``rule.pattern`` attribute access — that surface stays
  identical, but Pydantic gets us the schemas-file locality the rest of
  the config subsystem already uses. Argument extraction delegates to
  :class:`ember_code.core.config.tool_permissions.schemas.ToolInvocationArgs`
  so the two rule types share the primary-arg contract.

Rule-name friendly resolution is factored out into
:class:`FriendlyToolNameResolver` in :mod:`.resolver` — this module keeps
zero mutable state at import time.
"""

from __future__ import annotations

import fnmatch
import re
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ember_code.core.config.permission_eval.resolver import FriendlyToolNameResolver


class PermissionMode(StrEnum):
    """Top-level permission posture for a session.

    Defaults to ``DEFAULT``. ``DONT_ASK`` is the headless/CI mode
    (never prompt, deny unmatched). ``ACCEPT_EDITS`` auto-approves
    file mutation tools. ``BYPASS_PERMISSIONS`` runs without
    prompts unless an explicit deny / ask rule matches.
    ``PLAN`` forbids source edits entirely.
    """

    DEFAULT = "default"
    DONT_ASK = "dontAsk"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"


class PermissionDecision(Enum):
    """Outcome of one evaluation step.

    ``DEFER`` is the "no rule applies, fall through to the next
    step / canUseTool callback" value — needed because returning
    ``None`` for "no decision" mixed too easily with the other
    truthy/falsy answers in callers.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    DEFER = "defer"


#: Which pipeline step produced the outcome. Used by callers that want
#: to surface *why* a decision was made (e.g. "deny rule matched" vs
#: "plan mode blocked an edit tool") without a second scan.
PermissionOutcomeSource = Literal["deny", "ask", "mode", "allow", "defer"]


class PermissionOutcome(BaseModel):
    """Bundled result of one full pipeline evaluation.

    Bundling ``decision + reason + source`` in a single Pydantic model
    means ``explain_deny`` can read the reason produced by
    :meth:`PermissionEvaluator.evaluate_typed` instead of scanning the
    deny list a second time. This eliminates the two-scan drift the
    audit flagged (Pattern 3).
    """

    model_config = ConfigDict(frozen=True)

    decision: PermissionDecision
    reason: str
    source: PermissionOutcomeSource


# Matches ``ToolName`` or ``ToolName(pattern)`` rule strings. The
# tool name is ``[A-Za-z_][A-Za-z0-9_]*``; the pattern (when
# present) is everything between the parens, kept verbatim so
# globs / paths / quoted strings survive intact.
_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<pattern>.*)\))?$")


class PermissionRule(BaseModel):
    """A single ``Tool`` or ``Tool(pattern)`` rule.

    ``pattern is None`` means "bare-name rule" — matches any
    invocation of the tool regardless of arguments. A pattern
    matches the tool's most-distinctive string argument (``command``
    for shell, ``file_path``/``path`` for file tools) via
    ``fnmatch``.

    Sibling to :class:`ember_code.core.config.tool_permissions.schemas.PermissionRule`
    — the two types serve different purposes (this one drives the
    six-step Claude-Code pipeline, that one persists to the settings
    store with an extra ``level`` field). Consolidation is a
    documented follow-up; today they share primary-arg semantics
    via composition on :class:`ToolInvocationArgs`.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    pattern: str | None = None

    _RULE_RE: ClassVar[re.Pattern[str]] = _RULE_RE

    @classmethod
    def parse(cls, raw: str) -> PermissionRule | None:
        """Parse a string like ``"Bash"``, ``"Bash(npm test)"``,
        ``"Read(./.env)"``, or ``"*"`` (wildcard). Returns ``None``
        if the string can't be parsed — the caller should skip it
        with a warning rather than crash the whole pipeline."""
        raw = raw.strip()
        if not raw:
            return None
        if raw == "*":
            return cls(tool="*", pattern=None)
        m = cls._RULE_RE.match(raw)
        if not m:
            return None
        return cls(tool=m["tool"], pattern=m["pattern"])

    def display(self) -> str:
        """Format for use in reason strings — bare name or
        ``Tool(pattern)`` shape depending on whether the rule has a
        pattern."""
        if self.pattern:
            return f"{self.tool}({self.pattern})"
        return self.tool

    def deny_reason(self) -> str:
        """Human-readable reason for a step-2 deny-rule match."""
        return f"deny rule '{self.display()}' matched"

    @classmethod
    def parse_many(cls, raws: list[str]) -> list[PermissionRule]:
        """Parse every raw entry, silently dropping malformed ones.

        Replaces the old free ``_parse_rules`` helper. Callers can
        cross-check ``len(input) vs len(output)`` if they care.
        """
        parsed: list[PermissionRule] = []
        for raw in raws:
            rule = cls.parse(raw)
            if rule is not None:
                parsed.append(rule)
        return parsed

    def matches(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        resolver: FriendlyToolNameResolver | None = None,
    ) -> bool:
        """Does this rule match an invocation of ``tool_name`` with
        ``tool_args``? Wildcard tool (``*``) matches anything. A
        rule with no pattern matches any invocation of the named
        tool. With a pattern, the tool's primary string argument is
        fnmatched against the pattern.

        Tool-name matching is friendly-aware: a rule written as
        ``Bash(rm *)`` matches both the catalog name ``Bash`` AND
        the internal Agno function name ``run_shell_command``.
        Same for ``Edit`` ↔ ``edit_file`` / ``edit_file_replace_all``,
        ``Read`` ↔ ``read_file``, etc. Without this expansion a
        user-friendly rule using ``Bash`` silently fails to fire
        because the evaluator sees the internal name — exactly the
        bug that let ``rm -rf`` through under ``bypassPermissions``
        despite a ``deny: ["Bash(rm *)"]`` rule.

        ``resolver`` is optional to keep the direct-construction test
        surface (``PermissionRule(tool="Bash", pattern=None).matches(...)``)
        working without wiring; when omitted, we fall back to the
        shared default resolver so friendly-name expansion still fires.
        """
        if self.tool != "*" and self.tool != tool_name:
            active = resolver if resolver is not None else _default_resolver()
            if tool_name not in active.internals_for(self.tool):
                return False
        if self.pattern is None:
            return True
        target = _primary_arg(tool_name, tool_args)
        if target is None:
            return False
        return fnmatch.fnmatchcase(target, self.pattern)


def _primary_arg(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """Pick the argument we match the pattern against. Ordering
    matters: ``command`` first (shell tools), then ``file_path``,
    ``path``, ``filename`` — same priority the tool-event hook
    uses elsewhere for path harvesting.

    Kept here (not on :class:`ToolInvocationArgs`) because this rule
    type honours a *different* priority order than the sibling in
    ``tool_permissions/schemas.py``: this one prioritises ``command``
    first (six-step pipeline was built for shell rules), whereas
    that one prioritises ``args`` list first (fnmatch fallback).
    """
    for key in ("command", "file_path", "path", "filename", "url"):
        v = tool_args.get(key)
        if isinstance(v, str) and v:
            return v
    # ``args`` list (legacy shell tools): join with spaces so a
    # pattern like ``rm *`` matches ``["rm", "-rf", "build"]``.
    args_list = tool_args.get("args")
    if isinstance(args_list, list) and args_list:
        return " ".join(str(a) for a in args_list)
    return None


def _default_resolver() -> FriendlyToolNameResolver:
    """Lazy accessor for the process-wide default resolver.

    Imported inside the function to break the ``schemas → resolver →
    schemas`` circular reference at import time. The resolver itself
    is lazy about the underlying Agno registry lookup, so no work is
    done until a rule actually matches with a friendly-name mismatch.
    """
    from ember_code.core.config.permission_eval.resolver import (
        FriendlyToolNameResolver,
    )

    return FriendlyToolNameResolver.default()
