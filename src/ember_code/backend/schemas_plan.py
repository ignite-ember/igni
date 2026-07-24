"""Typed wire schemas for plan mode + todo panel.

Extracted from :mod:`ember_code.backend.server` — the
``LatestPlanResult`` model previously lived inline in the god-class
file. Sibling schemas modules (see ``schemas_run.py`` /
``schemas_pause.py``) are the pattern for backend wire types; this
file follows that convention.

The FE reads :class:`LatestPlanResult` on plan-panel open and after
every ``exit_plan_mode`` submission.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer

from ember_code.core.tools.todo import TodoItemWire

#: Display state of the plan-mode panel. ``""`` = no plan submitted;
#: ``"pending"`` = plan exists but the user hasn't clicked
#: approve/dismiss. Shared with :class:`schemas_history.PlanTurn` so
#: the plan-panel snapshot and the rebuilt-history turn speak one
#: vocabulary (single source of truth for the FE contract).
PlanState = Literal["pending", "approved", "dismissed", ""]


class LatestPlanResult(BaseModel):
    """Wire shape for :meth:`PlanSnapshotBuilder.latest` — the
    plan-mode panel reads this on open + after each ``exit_plan_mode``.

    ``state`` is ``"pending"`` when a plan exists (user hasn't
    approved/dismissed yet) or ``""`` when no plan submitted.
    ``tasks`` mirrors ``TodoStore.snapshot`` (activeForm camelCase
    via :class:`TodoItemWire` ``populate_by_name`` + ``alias=
    "activeForm"``) so the FE renders the plan and task list from a
    single payload.

    Wire-contract note: the RPC serializer
    (``message_dispatcher._serialize``) calls ``model_dump()``
    WITHOUT ``by_alias=True``. A bare ``list[TodoItemWire]`` field
    would therefore emit snake_case ``active_form`` and break the FE
    PlanCard checklist. The :meth:`_dump_tasks` field serializer
    forces ``by_alias=True`` per row so the wire JSON always carries
    camelCase ``activeForm`` regardless of how the outer model is
    dumped."""

    latest: str = ""
    history: list[str] = Field(default_factory=list)
    tasks: list[TodoItemWire] = Field(default_factory=list)
    state: PlanState = ""

    @field_serializer("tasks")
    def _dump_tasks(self, tasks: list[TodoItemWire]) -> list[dict[str, Any]]:
        # Force alias serialization so ``model_dump()`` (called
        # without ``by_alias`` by the RPC layer) still emits
        # ``activeForm`` camelCase — the FE wire contract.
        return [t.model_dump(by_alias=True) for t in tasks]
