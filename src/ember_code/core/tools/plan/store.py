"""Plan store — the per-session capture of ``exit_plan_mode`` submissions.

Split from the old flat ``plan.py`` (Rule-6 offender). Owns:

* :class:`PlanDecision` — StrEnum of the two valid user decisions;
  replaces the old ``_VALID_DECISIONS`` tuple + stringly-typed
  ``decision: str`` fields.
* :class:`PlanDecisionsBlob` — typed Pydantic model that
  persistence / rehydrate use as the wire shape for the
  ``{run_id: decision}`` map.
* :class:`PlanSnapshot` — wire shape for the ``GET_LATEST_PLAN`` RPC.
* :class:`PlanStore` — mutable per-session state; sole source of
  truth for plan approval/dismissal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanDecision(StrEnum):
    """User-recorded decision for a plan submitted via
    ``exit_plan_mode``. Concrete values match the historic
    wire strings so serialised blobs round-trip cleanly."""

    APPROVED = "approved"
    DISMISSED = "dismissed"


class PlanDecisionsBlob(BaseModel):
    """Typed shape for the persisted ``{run_id: decision}`` map.

    Consumed by :class:`SessionPersistence.load_plan_decisions` /
    :class:`SessionPersistence.save_plan_decisions` — the raw
    ``dict[str, str]`` shape is preserved via
    :meth:`model_dump` at the persistence boundary."""

    model_config = ConfigDict(extra="ignore")

    decisions: dict[str, PlanDecision] = Field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: Any) -> PlanDecisionsBlob:
        """Tolerant constructor. Filters non-string keys and
        non-enum values so a corrupt persisted blob doesn't raise
        — the store treats absence as "pending", the safe
        default."""
        cleaned: dict[str, PlanDecision] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if not isinstance(k, str) or not k:
                    continue
                try:
                    cleaned[k] = PlanDecision(v)
                except (TypeError, ValueError):
                    continue
        return cls(decisions=cleaned)


class PlanSnapshot(BaseModel):
    """Wire shape for :meth:`PlanStore.snapshot` — the latest plan
    the agent submitted plus the bounded history. Consumed by
    :meth:`BackendServer.get_latest_plan`; the FE renders ``latest``
    in the plan card and lets the user browse ``history`` in a
    detail view."""

    latest: str
    history: list[str]


@dataclass
class PlanStore:
    """Holds the most recent plan the agent submitted plus a
    short history (last few plans). Replaced atomically on each
    ``exit_plan_mode`` call — the agent sees the plan it just
    presented as the "latest", earlier ones move into history.

    Also tracks the user's per-plan decision (Approve / Refine
    button clicks) keyed by ``run_id`` — the run in which the
    agent called ``exit_plan_mode``. Persisted via
    :class:`SessionPersistence.save_plan_decisions` so reloads
    don't fall back to inferring approval from permission mode
    (the bug: a mode flip with no user click would silently mark
    a pending plan as approved).
    """

    latest: str = ""
    history: list[str] = field(default_factory=list)
    # ``run_id`` -> :class:`PlanDecision`. Absent key means the
    # user hasn't acted yet (pending). The mapping is the SOLE
    # source of truth for plan state — never inferred from mode,
    # never from message content.
    decisions: dict[str, PlanDecision] = field(default_factory=dict)
    # Max number of past plans we keep in history. Keeps memory
    # bounded — most sessions only ever have a handful, but a
    # /plan-toggle-heavy workflow could otherwise accumulate
    # indefinitely.
    _max_history: int = 10

    def set_plan(self, plan: str) -> None:
        if self.latest:
            self.history.append(self.latest)
            if len(self.history) > self._max_history:
                self.history = self.history[-self._max_history :]
        self.latest = plan

    def set_decision(self, run_id: str, decision: PlanDecision | str) -> None:
        """Record the user's decision for a specific plan
        (identified by the ``run_id`` of the run in which
        ``exit_plan_mode`` was called).

        ``decision`` must coerce to :class:`PlanDecision` —
        anything else raises so a typo in calling code surfaces
        immediately instead of silently corrupting the store.
        ``run_id`` must be a non-empty string for the same
        reason (an empty key would collide across plans).
        """
        if not run_id:
            raise ValueError("run_id must be non-empty")
        try:
            value = PlanDecision(decision)
        except (TypeError, ValueError) as exc:
            valid = tuple(d.value for d in PlanDecision)
            raise ValueError(f"decision must be one of {valid}, got {decision!r}") from exc
        self.decisions[run_id] = value

    def get_decision(self, run_id: str) -> str | None:
        """Return the recorded decision (as its wire string) or
        ``None`` if the user hasn't acted on this plan yet."""
        if not run_id:
            return None
        value = self.decisions.get(run_id)
        return value.value if value is not None else None

    def load_decisions(self, data: PlanDecisionsBlob | dict | None) -> None:
        """Bulk-load decisions from the persisted blob. Tolerates
        ``None`` / wrong-shaped values — anything that doesn't
        look like a ``str -> valid-decision`` mapping is
        dropped silently. Called on session rehydrate.

        Accepts either the raw dict shape (for back-compat) or a
        pre-validated :class:`PlanDecisionsBlob`.
        """
        if data is None:
            return
        blob = data if isinstance(data, PlanDecisionsBlob) else PlanDecisionsBlob.from_raw(data)
        self.decisions.update(blob.decisions)

    def decisions_snapshot(self) -> PlanDecisionsBlob:
        """Typed copy of the decisions map for persistence.
        Returning a fresh model (built from a dict copy) keeps
        the persistence layer from accidentally mutating store
        state when it serialises."""
        return PlanDecisionsBlob(decisions=dict(self.decisions))

    def snapshot(self) -> PlanSnapshot:
        """Wire shape for the panel / ``get_latest_plan`` RPC."""
        return PlanSnapshot(latest=self.latest, history=list(self.history))


__all__ = ["PlanDecision", "PlanDecisionsBlob", "PlanSnapshot", "PlanStore"]
