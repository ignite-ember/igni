"""Detect tool-side failures embedded in result strings.

Home for :class:`ToolResultErrorDetector` — the single source of
truth for "did this tool call fail, even though Agno didn't raise
a ``TOOL_ERROR`` event?".

Failure conventions we recognise
--------------------------------

Agno raises ``TOOL_ERROR_EVENTS`` for exceptions, but most
ember-code tools catch their failures and return an error message
as a string instead. Without flagging those, the TUI shows a green
``✓`` for a call the agent treats as a denial — the feedback loop
the user saw lying in v0.5.11.

We detect:

* ``"Error: ..."`` prefix — used by ``edit_file``,
  ``edit_file_replace_all``, ``create_file``, notebook tools,
  knowledge tools, codeindex tools.
* Shell-tool non-zero exit shapes:
  ``"Background process exited immediately (code N)"`` and
  ``"[Exited with code N after Ts]"``. Code 0 is success and
  must not flag.

Strict prefix / regex anchors keep this from misfiring on body
content (e.g. a Read of a file that legitimately contains
``"Error:"`` or ``"Exited with code 1"`` mid-line).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# ── Default patterns ──────────────────────────────────────────────

# Shell tool's failure conventions — non-zero exit code in either
# of its two output shapes. We keep these as explicit patterns
# instead of a generic "contains 'exit'" heuristic to avoid false
# positives on a Read of a log file or a grep result.
_DEFAULT_SHELL_BG_FAIL_RE = re.compile(r"^Background process exited immediately \(code (\d+)\)")
_DEFAULT_SHELL_EXIT_FAIL_RE = re.compile(r"^\[Exited with code (\d+)")

_DEFAULT_ERROR_PREFIXES: tuple[str, ...] = ("Error:",)
_DEFAULT_FAILURE_REGEXES: tuple[re.Pattern[str], ...] = (
    _DEFAULT_SHELL_BG_FAIL_RE,
    _DEFAULT_SHELL_EXIT_FAIL_RE,
)


class ToolResultErrorDetector:
    """Owns the "does this string look like a tool failure?" rules.

    Configured via constructor args so tests / third-party tool
    integrations can inject additional conventions without editing
    this module. The production defaults are exposed via
    :meth:`default` so callers get one-liner composition (mirrors
    the sibling :class:`AgnoToolEventFormatter` factory shape and
    avoids a hidden module-level singleton).

    Instance fields hold the regex + prefix bundles — no module
    globals — so a second detector with different rules can live
    side-by-side (e.g. a test detector that also flags a custom
    tool convention).
    """

    def __init__(
        self,
        error_prefixes: Sequence[str] = _DEFAULT_ERROR_PREFIXES,
        failure_regexes: Sequence[re.Pattern[str]] = _DEFAULT_FAILURE_REGEXES,
    ) -> None:
        self._error_prefixes: tuple[str, ...] = tuple(error_prefixes)
        self._failure_regexes: tuple[re.Pattern[str], ...] = tuple(failure_regexes)

    @classmethod
    def default(cls) -> ToolResultErrorDetector:
        """Return the production detector — ember-code's shell +
        ``Error:`` prefix bundle.

        Mirrors :meth:`AgnoToolEventFormatter.__init__`'s default
        composition shape: a class-level factory rather than a
        module-level singleton. Callers that want isolation build
        their own; the module never mutates shared state.
        """
        return cls(
            error_prefixes=_DEFAULT_ERROR_PREFIXES,
            failure_regexes=_DEFAULT_FAILURE_REGEXES,
        )

    def is_error(self, result: str) -> bool:
        """Return True when ``result`` matches any configured
        failure convention.

        Strict left-anchored checks — prefixes via
        ``str.startswith`` and regexes via ``re.match`` — so mid-
        line occurrences of ``"Error:"`` inside a legitimate Read
        or grep result never misfire.
        """
        if not result:
            return False
        stripped = result.lstrip()
        for prefix in self._error_prefixes:
            if stripped.startswith(prefix):
                return True
        for rx in self._failure_regexes:
            m = rx.match(stripped)
            if m:
                # Regexes capture the exit-code digit in group 1;
                # non-zero → failure, 0 → success. When a regex has
                # no group we treat any match as failure (the
                # convention was configured to flag on match).
                if not m.groups():
                    return True
                if m.group(1) != "0":
                    return True
        return False


__all__ = ["ToolResultErrorDetector"]
