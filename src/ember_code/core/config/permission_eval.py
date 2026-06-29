"""Claude Code-style 6-mode tool permission evaluator.

Pure module: parses ``Tool(pattern)`` rules, holds a
``PermissionMode``, and walks the 6-step evaluation pipeline
(``hooks → deny → ask → mode → allow → defer``). No I/O, no
network, no interactive prompts — those happen in the layer that
calls this evaluator (the tool-event hook, eventually a UI bridge).

Modelled on `code.claude.com/docs/en/agent-sdk/permissions`. The
TS-only ``auto`` mode (model classifier) is intentionally absent
from the Python surface.

The key safety invariant: a deny rule with a scope pattern (e.g.
``Bash(rm *)``) STILL blocks matching invocations in
``bypassPermissions`` mode. Only bare-name denies (e.g. plain
``Bash``) follow the "remove the tool from context" shortcut and
that lives at a different layer.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


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


# Tools that mutate the filesystem — used by ``acceptEdits``
# (auto-approve) and ``plan`` (block). Names match Claude Code's
# Edit/Write/NotebookEdit set, plus ember-code's ``edit_file_*`` /
# ``save_file`` / ``create_file`` variants. Bash is handled by a
# separate heuristic (see ``_bash_command_mutates``) because shell
# commands may or may not mutate.
FILE_EDIT_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "NotebookEdit",
        "save_file",
        "edit_file",
        "edit_file_replace_all",
        "create_file",
    }
)

# Tools that only read — auto-allowed in plan mode so the agent can
# investigate without prompting. Catalog names + internal Agno
# function names (the evaluator may see either depending on the
# call site).
FILE_READ_TOOLS = frozenset(
    {
        "Read",
        "read_file",
        "read_file_chunk",
        "Grep",
        "grep",
        "grep_files",
        "grep_count",
        "Glob",
        "glob_files",
        "LS",
        "list_files",
        "WebSearch",
        "duckduckgo_search",
        "duckduckgo_news",
        "WebFetch",
        "fetch_url",
        "fetch_json",
        "CodeIndex",
        "codeindex_query",
        "codeindex_tree",
    }
)

# Shell tools — checked specially in plan mode via mutation heuristic.
SHELL_TOOLS = frozenset({"Bash", "run_shell_command"})

# Mutation markers in a shell command string. Plan mode denies any
# Bash call whose command matches one of these. Conservative by
# design: false positives just route to the user prompt, false
# negatives let an edit slip through plan mode (worse).
#
# Covered: in-place edits (``sed -i``, ``perl -i``), redirects to a
# file (``>``, ``>>``), and obvious filesystem-mutating verbs as the
# first token after ``;`` / ``&&`` / ``|`` / start-of-command. Stderr
# merges like ``2>&1`` don't trigger because the regex requires a
# non-``&`` character after ``>``.
_BASH_MUTATION_RE = re.compile(
    r"(?:^|[\s|;&(`])"  # start-of-command or shell separator
    r"(?:rm|rmdir|mv|cp|mkdir|touch|chmod|chown|ln|dd|truncate|tee)\b"
    r"|\bsed\s+-i\b"
    r"|\bperl\s+-i\b"
    r"|>\s*[^&\s|]"  # write redirect to a path (not >& or > |)
    r"|>>"
)


def _bash_command_mutates(tool_args: dict[str, Any]) -> bool:
    """Heuristic: does this Bash invocation write to the filesystem?

    Reads ``command`` (ember-code's ``run_shell_command`` arg) or
    falls back to ``args`` (legacy ``ShellTools`` arg). Returns
    ``True`` for any match of :data:`_BASH_MUTATION_RE`.
    """
    cmd_str = ""
    raw = tool_args.get("command")
    if isinstance(raw, str):
        cmd_str = raw
    elif isinstance(raw, list):
        cmd_str = " ".join(str(p) for p in raw)
    else:
        args = tool_args.get("args")
        if isinstance(args, list):
            cmd_str = " ".join(str(p) for p in args)
        elif isinstance(args, str):
            cmd_str = args
    if not cmd_str:
        return False
    return bool(_BASH_MUTATION_RE.search(cmd_str))


# Matches ``ToolName`` or ``ToolName(pattern)`` rule strings. The
# tool name is ``[A-Za-z_][A-Za-z0-9_]*``; the pattern (when
# present) is everything between the parens, kept verbatim so
# globs / paths / quoted strings survive intact.
_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<pattern>.*)\))?$")


@dataclass(frozen=True)
class PermissionRule:
    """A single ``Tool`` or ``Tool(pattern)`` rule.

    ``pattern is None`` means "bare-name rule" — matches any
    invocation of the tool regardless of arguments. A pattern
    matches the tool's most-distinctive string argument (``command``
    for shell, ``file_path``/``path`` for file tools) via
    ``fnmatch``.
    """

    tool: str
    pattern: str | None

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
        m = _RULE_RE.match(raw)
        if not m:
            return None
        return cls(tool=m["tool"], pattern=m["pattern"])

    def matches(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """Does this rule match an invocation of ``tool_name`` with
        ``tool_args``? Wildcard tool (``*``) matches anything. A
        rule with no pattern matches any invocation of the named
        tool. With a pattern, the tool's primary string argument is
        fnmatched against the pattern."""
        if self.tool != "*" and self.tool != tool_name:
            return False
        if self.pattern is None:
            return True
        target = _primary_arg(tool_name, tool_args)
        if target is None:
            return False
        return fnmatch.fnmatchcase(target, self.pattern)


def explain_deny(
    evaluator: PermissionEvaluator,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """Human-readable reason an ``evaluate(...)`` came back DENY.

    Threaded into the tool-rejection note so the **agent** — not just
    the user reading the dialog — knows what to do next. Without
    context the model treated a generic "Blocked by permission
    policy" as a hostile environment and asked the user to run the
    command manually; with this, it sees "plan mode blocks edits,
    call exit_plan_mode(plan) when ready" and routes correctly.
    """
    # Step 2: a deny rule matched
    for rule in evaluator.deny:
        if rule.matches(tool_name, tool_args):
            display = rule.tool
            if rule.pattern:
                display = f"{rule.tool}({rule.pattern})"
            return f"deny rule '{display}' matched"

    # Step 4: mode-specific shortcuts
    if evaluator.mode is PermissionMode.PLAN:
        if tool_name in FILE_EDIT_TOOLS:
            return (
                "plan mode blocks file edits. Use exit_plan_mode(plan) "
                "when you're ready for the user to approve execution."
            )
        if tool_name in SHELL_TOOLS:
            return (
                "plan mode blocks mutating shell commands (rm, mv, cp, "
                "mkdir, sed -i, > redirect, …). Read-only shell calls "
                "are fine. Use exit_plan_mode(plan) when ready to execute."
            )

    if evaluator.mode is PermissionMode.DONT_ASK:
        return f"headless mode (dontAsk) and {tool_name} is not in the allow list"

    return f"{tool_name} is denied by policy"


def _primary_arg(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """Pick the argument we match the pattern against. Ordering
    matters: ``command`` first (shell tools), then ``file_path``,
    ``path``, ``filename`` — same priority the tool-event hook
    uses elsewhere for path harvesting."""
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


@dataclass
class PermissionEvaluator:
    """The 6-step evaluation pipeline.

    Order (matching Claude Code's contract):
      1. ``hooks`` — fired by the tool-event hook BEFORE this
         evaluator runs; not modelled here.
      2. ``deny`` — any matching deny rule → ``DENY``. Bypass-
         resistant: still wins in ``bypassPermissions`` mode.
      3. ``ask`` — any matching ask rule → ``ASK`` (caller asks
         the user / canUseTool).
      4. ``mode`` — mode-specific shortcut: ``acceptEdits``
         auto-allows file-edit tools, ``plan`` denies them,
         ``bypassPermissions`` allows everything not already
         denied/asked, ``dontAsk`` denies anything not allowed.
      5. ``allow`` — any matching allow rule → ``ALLOW``.
      6. ``defer`` — return ``DEFER`` so the caller routes to its
         interactive/UI/canUseTool fallback.
    """

    mode: PermissionMode = PermissionMode.DEFAULT
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    allow: list[PermissionRule] = field(default_factory=list)

    @classmethod
    def from_strings(
        cls,
        mode: str | PermissionMode = PermissionMode.DEFAULT,
        deny: list[str] | None = None,
        ask: list[str] | None = None,
        allow: list[str] | None = None,
    ) -> PermissionEvaluator:
        """Convenience constructor — accepts raw strings from
        ``settings.permissions`` and parses them into
        ``PermissionRule`` objects, silently dropping malformed
        entries (caller can check the lengths if it cares)."""
        return cls(
            mode=PermissionMode(mode) if isinstance(mode, str) else mode,
            deny=_parse_rules(deny or []),
            ask=_parse_rules(ask or []),
            allow=_parse_rules(allow or []),
        )

    def evaluate(self, tool_name: str, tool_args: dict[str, Any]) -> PermissionDecision:
        # Step 2: deny
        if _any_match(self.deny, tool_name, tool_args):
            return PermissionDecision.DENY

        # Step 3: ask
        if _any_match(self.ask, tool_name, tool_args):
            return PermissionDecision.ASK

        # Step 4: mode-specific shortcuts
        mode_decision = self._mode_step(tool_name, tool_args)
        if mode_decision is not PermissionDecision.DEFER:
            return mode_decision

        # Step 5: allow
        if _any_match(self.allow, tool_name, tool_args):
            return PermissionDecision.ALLOW

        # Step 6: defer (caller's canUseTool / interactive prompt)
        if self.mode is PermissionMode.DONT_ASK:
            # Headless mode: no prompts means anything unmatched
            # at this point is a deny, not a defer.
            return PermissionDecision.DENY
        return PermissionDecision.DEFER

    def _mode_step(self, tool_name: str, tool_args: dict[str, Any]) -> PermissionDecision:
        is_edit_tool = tool_name in FILE_EDIT_TOOLS
        is_read_tool = tool_name in FILE_READ_TOOLS
        is_shell_tool = tool_name in SHELL_TOOLS

        if self.mode is PermissionMode.PLAN:
            if is_edit_tool:
                return PermissionDecision.DENY
            if is_shell_tool:
                # Shell commands may or may not mutate. In plan mode
                # block the obvious writers (sed -i, > redirect, rm,
                # mv, cp, ...) and let read-only shell calls through.
                if _bash_command_mutates(tool_args):
                    return PermissionDecision.DENY
                return PermissionDecision.ALLOW
            if is_read_tool:
                return PermissionDecision.ALLOW
            # Custom / unknown tool — fall through to step 5/6 so the
            # user can decide. Don't auto-allow what we don't classify.
            return PermissionDecision.DEFER
        if self.mode is PermissionMode.ACCEPT_EDITS and is_edit_tool:
            return PermissionDecision.ALLOW
        if self.mode is PermissionMode.BYPASS_PERMISSIONS:
            # Anything not already denied or asked is auto-allowed.
            return PermissionDecision.ALLOW
        return PermissionDecision.DEFER


def _parse_rules(raws: list[str]) -> list[PermissionRule]:
    parsed: list[PermissionRule] = []
    for raw in raws:
        rule = PermissionRule.parse(raw)
        if rule is not None:
            parsed.append(rule)
    return parsed


def _any_match(rules: list[PermissionRule], tool_name: str, tool_args: dict[str, Any]) -> bool:
    return any(r.matches(tool_name, tool_args) for r in rules)
