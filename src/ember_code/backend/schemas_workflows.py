"""Typed schemas for the workflow runner surface.

Three concerns:

1. ``WorkflowMeta`` — what a workflow advertises (read from the
   ``meta`` export of every ``.claude/workflows/*.mjs``). Mirrors
   the CC convention: ``{name, description, whenToUse, phases}``.

2. ``WorkflowEvent`` — the envelope the Node subprocess emits
   one JSON line at a time on stdout. Every event has a
   ``run_id`` (so the BE can route re-connections) and a
   monotonic ``seq`` (so the FE can re-order if a push
   arrives out of order).

3. ``WorkflowRunRequest`` / ``WorkflowRunResponse`` — the RPC
   contract for ``LIST_WORKFLOWS`` and ``RUN_WORKFLOW``.

Following the same pattern as
:mod:`ember_code.backend.schemas_rpc`: every wire shape is a
Pydantic model, every construction site uses ``model_dump()`` at
the boundary, shape drift fails loud at validation time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Discovery (LIST_WORKFLOWS) ─────────────────────────────────────


class WorkflowPhase(BaseModel):
    """One phase in a workflow's published ``meta.phases`` list."""

    model_config = ConfigDict(extra="forbid")

    title: str
    detail: str = ""


class WorkflowMeta(BaseModel):
    """The ``meta`` export of a workflow file.

    Mirrors the CC convention. The runner reads this once at
    discovery and surfaces it to the FE for the workflow
    autocomplete + describe panel.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    whenToUse: str = ""
    phases: list[WorkflowPhase] = Field(default_factory=list)


class WorkflowMetaEnvelope(BaseModel):
    """One row of the ``LIST_WORKFLOWS`` response.

    Wraps :class:`WorkflowMeta` with the on-disk path so the FE
    can deep-link / show the source. ``path`` is relative to
    the project root (typically ``.claude/workflows/<name>.mjs``).
    """

    model_config = ConfigDict(extra="forbid")

    meta: WorkflowMeta
    path: str


# ── Runtime event envelope (subprocess stdout) ─────────────────────


class WorkflowEvent(BaseModel):
    """One JSON line emitted by ``workflow_runtime.mjs`` on stdout.

    The BE writes one of these into ``session.append_event`` per
    line, with the event_type normalized to ``"workflow_event"``,
    and pushes it on the ``workflow_event`` push channel.

    ``payload`` is intentionally a free-form dict — each event
    ``type`` has its own shape, and the FE reducer folds by type.
    Putting every variant on this model would force both ends to
    keep a giant ``Union`` in sync; the FE is happy to do
    ``switch(ev.type)`` on the raw dict.
    """

    model_config = ConfigDict(extra="forbid")

    ts: int
    run_id: str
    seq: int = Field(ge=0)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ── RPC contracts (LIST_WORKFLOWS / RUN_WORKFLOW) ──────────────────


class WorkflowRunRequest(BaseModel):
    """``RUN_WORKFLOW`` params."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunResponse(BaseModel):
    """``RUN_WORKFLOW`` response.

    Returned synchronously — the run is async on the BE side. The
    FE optimistically creates a workflow ``ChatItem`` keyed by
    ``workflow_run_id`` and folds subsequent ``workflow_event``
    pushes into it.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    name: str


# ── Status (used by FE reducer; also stable across the wire) ──────


WorkflowStatus = Literal["running", "completed", "failed", "cancelled"]
