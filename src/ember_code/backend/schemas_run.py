"""Typed schemas for the run pipeline (RunController + collaborators).

Extracted out of :mod:`ember_code.backend.server_run` — the old
free-function module used raw dicts for hook payloads, ``dict[str,
Any]`` for media kwargs, and a bare bool for the run's lifecycle
state. Every dict / bool that flows across a run-pipeline boundary
lives here as a Pydantic model so mypy + Ruff + Pydantic validation
give us schema coverage at every seam.

Consumers:

* :class:`RunPhase` — replaces ``BackendServer._processing`` bool.
  ``is_active`` is what the FE's "processing?" question maps to.
* :class:`MediaAttachments` — the ``media`` argument to
  ``run_message`` used to be ``dict[str, Any]``; now a typed model.
  Moved down to :mod:`ember_code.core.utils.media_schemas` so
  :class:`~ember_code.core.utils.media.MediaResolver` can produce
  it directly (previously ``core/utils`` returned a raw dict and
  ``backend`` validated it — a layer inversion). Re-exported from
  here for wire-compat with pre-refactor importers.
* :class:`UserPromptSubmitPayload` / :class:`StopHookPayload` — the
  hook fire payloads. Consumers include both the RunController
  natural-end path and :meth:`HitlController.resolve_batch` after
  ``acontinue_run`` finishes.
* :class:`ModelConfig` — one entry in ``settings.models.registry``.
  The registry is still ``dict[str, dict]`` on disk but we validate
  the row-shape at the point of use in RunController.
* :class:`HttpClientCloseResult` — the previously-swallowed
  ``except Exception`` in ``close_model_http_client`` becomes a
  typed Result.
* :class:`PromptBuildResult` / :class:`HookGateResult` — the
  return types of the two run-pipeline collaborators
  (``PromptBuilder`` and ``RunHookGate``) so the RunController's
  main body reads as typed dataflow, not orchestration of anonymous
  tuples.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Re-export so ``from ember_code.backend.schemas_run import MediaAttachments``
# still works after the media schema moved down into ``core/utils``.
from ember_code.core.utils.media_schemas import MediaAttachments
from ember_code.protocol import messages as msg

__all__ = [
    "CancelAgentRunResult",
    "HookGateResult",
    "HttpClientCloseResult",
    "MediaAttachments",
    "ModelConfig",
    "PromptBuildResult",
    "RunPhase",
    "StopHookPayload",
    "UserPromptSubmitPayload",
]


class RunPhase(str, Enum):
    """Lifecycle state of a single ``run_message`` invocation.

    Replaces the ``BackendServer._processing`` bool + the three
    scattered ``_processing = ...`` assignments in the old
    ``server_run.py``. Every state is actually reached by
    :meth:`RunController._transition_to` — no dead values.

    Only :meth:`is_active` participates in the wire contract today —
    the FE's ``get_processing`` RPC (see ``BackendServer.processing``)
    maps to ``phase.is_active()``.
    """

    idle = "idle"
    starting = "starting"
    streaming = "streaming"
    finalizing = "finalizing"
    errored = "errored"
    done = "done"

    def is_active(self) -> bool:
        """True when a run is holding state a second concurrent submit
        would race on. ``done`` / ``errored`` / ``idle`` are all
        safely quiescent."""
        return self in (RunPhase.starting, RunPhase.streaming, RunPhase.finalizing)


class UserPromptSubmitPayload(BaseModel):
    """Payload dict for ``UserPromptSubmit`` hook fires.

    Replaces the ``{"message": text, "session_id": ...}`` dict
    literal in the old ``run_message_locked``. Consumers still see a
    plain dict at the hook-executor interface (``.model_dump()`` at
    the boundary)."""

    message: str
    session_id: str


class StopHookPayload(BaseModel):
    """Payload dict for the ``Stop`` hook fires.

    Both callsites (Stop hook after natural end-of-run in
    :class:`RunController`, Stop hook after
    :meth:`HitlController.resolve_batch`) share one type."""

    session_id: str


class ModelConfig(BaseModel):
    """One row of ``settings.models.registry``.

    The registry is still ``dict[str, dict[str, Any]]`` on disk (see
    :class:`ember_code.core.config.settings.ModelsConfig`), but we
    validate the row-shape at the RunController boundary so the
    ``.vision`` check is a real attribute lookup rather than
    ``.get('vision', False)``. ``extra='allow'`` preserves any
    provider-specific keys we haven't named."""

    model_config = ConfigDict(extra="allow")

    vision: bool = False
    model_id: str = ""


class HttpClientCloseResult(BaseModel):
    """Result of :meth:`ModelHttpClientManager.close_and_replace`.

    The old free ``close_model_http_client`` swallowed every
    exception with a ``log.debug`` and continued. We keep the
    "always succeed and replace" semantic but surface the reason on
    a failure so callers (or tests) can pin what went wrong."""

    ok: bool
    reason: str = ""


class CancelAgentRunResult(BaseModel):
    """Wire shape for :meth:`RunCoordinator.cancel_agent_run` —
    the FE renders a toast on ``ok=False`` so ``error``
    differentiates the unknown-run-id case from a live cancel
    error."""

    ok: bool
    error: str = ""


class HookGateResult(BaseModel):
    """Return type of :class:`RunHookGate` methods.

    ``should_continue`` mirrors the underlying hook executor's field
    of the same name — ``False`` means the RunController must yield
    an Error and bail before ``team.arun``. ``block_message`` is
    what to put in the Error text. ``context_message`` is the
    optional string to append as a ``<hook-context>`` block into the
    prompt when ``should_continue`` is True.
    """

    should_continue: bool
    block_message: str | None = None
    context_message: str | None = None


class PromptBuildResult(BaseModel):
    """Return type of :meth:`PromptBuilder.build`.

    ``message`` is the fully-assembled prompt with
    ``<system-context>`` framing, ready for ``team.arun``.
    ``media`` is the (possibly enriched) attachments bundle after
    file resolution + URL extraction. ``info_messages`` is the
    ordered list of user-visible ``msg.Info`` announcements the
    RunController yields to the FE BEFORE calling ``team.arun`` —
    "Referenced: X", "Attached N URL(s)", "(continuing from …)",
    etc. Yielding them here (rather than sprinkling ``yield`` calls
    inside the builder) keeps the builder a pure function and
    leaves the RunController in charge of the wire order."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message: str
    media: MediaAttachments | None
    info_messages: list[msg.Info] = Field(default_factory=list)
