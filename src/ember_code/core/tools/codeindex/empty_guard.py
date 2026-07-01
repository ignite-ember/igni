"""Empty-call guardrail for ``codeindex_query``.

The agent's case-11-shape failure looks like
``codeindex_query(security=None, sections=[…], limit=15)`` — it reached
for the right tool, named the dimension it wanted to triage on, but
passed ``None`` instead of an actual list of severities. The call
returns arbitrary ranked items; the agent reads them as "the worst
offenders" and confabulates a triage. This module's :func:`is_empty_call`
detects that shape so the call site can return a didactic error.
"""

from __future__ import annotations

from typing import Any


def is_empty_call(**kwargs: Any) -> bool:
    """True iff ``codeindex_query`` was invoked with no narrowing input.

    A call is "empty" when ALL of:
      - no ``query_text``
      - no ``ids``
      - every typed-filter arg is ``None`` (or an empty list, for the
        list-shaped multi-value categories).

    NOTE: caller responsibility — this helper doesn't know which
    kwargs are output-control vs narrowing. The only call site
    (``query_service.codeindex_query``) excludes ``sections`` /
    ``limit`` / ``commit`` from the kwargs it forwards here; if you
    add a new caller, do the same. Passing output-control kwargs
    in would make a non-empty list count as narrowing and silently
    defeat the detection. See ``tests/test_empty_guard.py``.
    """
    if kwargs.get("query_text"):
        return False
    if kwargs.get("ids"):
        return False
    for name, value in kwargs.items():
        if name in ("query_text", "ids"):
            continue
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        # bool ``needs_refactoring`` is meaningful even when False — but
        # ``False`` filters to "items that don't need refactoring," which
        # is a real (if unusual) query, so accept it.
        return False
    return True
