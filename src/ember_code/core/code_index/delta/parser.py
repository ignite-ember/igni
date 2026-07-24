"""JSONL → :data:`DeltaOp` parsing.

:class:`DeltaParser` owns a single ``TypeAdapter[DeltaOp]`` and exposes
two methods: :meth:`parse_line` (one JSONL line → one op or ``None``
for blanks) and :meth:`iter_ops` (streams a whole file, wraps line-
number context around any :class:`DeltaError`).

Fixes the audit's utility-module-of-related-helpers offender: the two
functions used to share an implicit subject (the wire schema
registry). They now share an explicit one — the parser instance.

The ``TypeAdapter`` lives as a ``ClassVar`` so validation-plus-
discrimination is a single Pydantic call. No module-level ``_OP_MODELS``
dict, no two-step name-then-model lookup, and — because the adapter is
built off :data:`DeltaOp` — the isinstance-vs-dict inconsistency from
the audit's Pattern-2 note is eliminated by construction.

The constructor accepts an optional ``type_adapter`` so a future test
or plugin can inject an extended op union without patching module
globals.

Error message substrings (``invalid JSON``, ``missing 'op'``, ``unknown
op``, ``validation failed``) are preserved verbatim so the existing
``pytest.raises(DeltaError, match=...)`` assertions keep passing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from pydantic import TypeAdapter, ValidationError

from ember_code.core.code_index.delta.ops import DeltaOp
from ember_code.core.code_index.delta.stats import DeltaError


class DeltaParser:
    """Parse JSONL lines into typed :data:`DeltaOp` instances."""

    _ADAPTER: ClassVar[TypeAdapter[DeltaOp]] = TypeAdapter(DeltaOp)

    def __init__(self, type_adapter: TypeAdapter[DeltaOp] | None = None) -> None:
        # Instance-level override for plugin/test injection; defaults
        # to the shared class-level adapter so the common case
        # allocates nothing extra.
        self._adapter = type_adapter or self._ADAPTER

    def parse_line(self, raw: str) -> DeltaOp | None:
        """Parse one JSONL line into the matching op model.

        Returns ``None`` for blank or whitespace-only lines (JSONL
        files legitimately contain them between records). Raises
        :class:`DeltaError` for every malformed case — invalid JSON,
        missing ``op`` field, unknown op name, or Pydantic validation
        failure against the discriminated union.
        """
        raw = raw.strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeltaError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "op" not in payload:
            raise DeltaError(f"missing 'op' field: {raw[:120]}")
        op_name = payload["op"]
        try:
            return self._adapter.validate_python(payload)
        except ValidationError as exc:
            # Pydantic's discriminator error mentions ``tag`` — translate
            # to the historical ``unknown op`` message so tests keep
            # passing without changing their ``match=`` string.
            if self._is_unknown_op_error(exc):
                raise DeltaError(f"unknown op: {op_name!r}") from exc
            raise DeltaError(f"validation failed for {op_name}: {exc}") from exc

    def iter_ops(self, jsonl_path: str | Path) -> Iterator[DeltaOp]:
        """Yield parsed ops from a JSONL file, skipping blank lines.

        Wraps line-number context around any :class:`DeltaError` raised
        during parsing so caller stack traces point at the offending
        line rather than the middle of the applier's dispatch loop.
        """
        path = Path(str(jsonl_path)).expanduser()
        with path.open() as fh:
            for line_no, line in enumerate(fh, start=1):
                try:
                    op = self.parse_line(line)
                except DeltaError as exc:
                    raise DeltaError(f"line {line_no}: {exc}") from exc
                if op is not None:
                    yield op

    @staticmethod
    def _is_unknown_op_error(exc: ValidationError) -> bool:
        """Detect Pydantic's ``union_tag_invalid`` error kind.

        Pydantic surfaces an unknown discriminator value as a single
        error of type ``union_tag_invalid``. Anything else (missing
        field, wrong type, extra=forbid) is a real validation failure
        that gets the generic ``validation failed`` message instead.
        """
        return any(err.get("type") == "union_tag_invalid" for err in exc.errors())
