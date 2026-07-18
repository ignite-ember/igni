"""Delta-apply outcome types.

Three sibling public types live here:

- :class:`DeltaStats` — counters mutated during a single
  :class:`~ember_code.core.code_index.delta.applier.DeltaApplier` run.
- :class:`DeltaResult` — a Pattern-3 Result wrapper carrying
  ``ok`` / ``reason`` / ``stats`` returned by
  :meth:`DeltaApplier.run`. Callers that want richer error handling
  than the module-level shim exposes can consume the applier directly
  and pattern-match on this.
- :class:`DeltaError` — raised when the JSONL is malformed in a way
  the applier can't recover from (invalid JSON, missing ``op`` field,
  unknown op, validation error, missing commit header, second commit
  header, empty file, or an unrecoverable I/O failure). Kept for
  backward compat with existing ``pytest.raises(DeltaError, match=...)``
  test assertions — those expected-failure paths still raise verbatim.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DeltaStats(BaseModel):
    """Counters mutated during a single :class:`DeltaApplier` run.

    All fields default to zero / False so ``DeltaStats()`` is a
    valid empty starting state (matches the previous ``@dataclass``
    behaviour that call sites depend on).
    """

    items_upserted: int = 0
    items_deleted: int = 0
    references_upserted: int = 0
    references_deleted: int = 0
    skipped_lines: int = 0
    commit_summary_written: bool = False


class DeltaResult(BaseModel):
    """Structured outcome of a :class:`DeltaApplier` run.

    Mirrors :class:`~ember_code.core.code_index.fetcher.PreflightResult`
    so richer callers can pattern-match on ``ok`` / ``reason`` instead
    of catching :class:`DeltaError`. The module-level ``apply_delta``
    shim unwraps ``result.stats`` for the current call sites in
    ``index.py`` / ``fetcher.py`` / ``sync_manager.py`` — migration
    to this Result type at those call sites is a follow-up.
    """

    ok: bool
    reason: str = ""
    stats: DeltaStats = Field(default_factory=DeltaStats)


class DeltaError(Exception):
    """Raised when the JSONL is malformed in a way the applier can't recover from."""
