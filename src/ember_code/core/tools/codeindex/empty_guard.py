"""Empty-call guardrail for ``codeindex_query`` — thin shim over :class:`QueryInput`.

The real check lives on :meth:`QueryInput.is_empty_call` — the model
owns the field list, so the check can enumerate narrowing fields
directly instead of relying on caller discipline about which kwargs
to forward.

This module survives as a back-compat facade for the historical
``from ember_code.core.tools.codeindex.empty_guard import is_empty_call``
import path. Existing tests keep working; new code should use
:meth:`QueryInput.is_empty_call` directly.
"""

from __future__ import annotations

from typing import Any

from ember_code.core.tools.codeindex.schemas import _NARROWING_FIELDS


def is_empty_call(**kwargs: Any) -> bool:
    """True iff ``codeindex_query`` was invoked with no narrowing input.

    Mirrors :meth:`QueryInput.is_empty_call` semantics. Implemented as
    a kwargs-only check so tests can pass raw strings for enum-typed
    fields without tripping Pydantic validation — the shim only reads
    narrowing-field presence, never coerces values.
    """
    if kwargs.get("query_text"):
        return False
    if kwargs.get("ids"):
        return False
    for name in _NARROWING_FIELDS:
        if name in ("query_text", "ids"):
            continue
        value = kwargs.get(name)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        return False
    return True
