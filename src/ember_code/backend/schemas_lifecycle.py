"""Typed schemas for the lifecycle pipeline (startup / interrupted-run /
shutdown).

Extracted from :mod:`ember_code.backend.server_lifecycle` — the
previous module passed a bare ``(str, list[str])`` pair to
``RunController.set_interrupted_summary`` and lowered
``BackendServer.shutdown`` cleanup steps through four opaque
``contextlib.suppress(Exception)`` blocks with no observable
outcome. Every dict / tuple / silent-swallow that flows across a
lifecycle boundary lives here as a Pydantic model so mypy + Ruff +
Pydantic validation give schema coverage at every seam.

Sibling convention: mirrors :mod:`schemas_run` / :mod:`schemas_hitl`
/ :mod:`schemas_history` — one schemas module per top-level
pipeline.

Consumers:

* :class:`InterruptedRunSummary` — replaces the loose
  ``(summary_text: str, ids_to_drop: list[str])`` pair currently
  handed from ``LifecycleController.detect_interrupted_run`` to
  ``RunController.set_interrupted_summary``. One value, one type.
* :class:`AgnoRunSnapshot` — boundary adapter that absorbs Agno's
  dynamic attribute shape once (``run_id`` / ``status`` / ``tools``
  / ``content``) so the rest of the lifecycle code reads real
  fields instead of scattered ``getattr(..., None)`` probes.
* :class:`SessionEndPayload` — the typed hook payload replacing
  the raw ``{'session_id': ...}`` dict at
  ``LifecycleController.shutdown``. Semantically distinct from
  :class:`StopHookPayload` in :mod:`schemas_run` (SESSION_END fires
  on server shutdown; STOP fires per-run) — they happen to share a
  shape today but the domains diverge so keep them separate.
* :class:`ShutdownResult` — typed replacement for the four
  ``contextlib.suppress(Exception)`` blocks inside
  ``LifecycleController.shutdown``. Cleanup failures become
  observable outcomes (``errors`` list) instead of silent
  swallows.
* :class:`RehydrateOutcome` — typed per-step result returned by
  :class:`RehydrateController` methods. Replaces the seven bare
  ``except Exception → logger.debug → return`` swallows that used
  to hide startup-recovery failures. :meth:`LifecycleController.startup`
  accumulates the five outcomes and emits a single INFO summary
  so failed steps are observable without breaking the best-effort
  contract (no re-raise). Scoped strictly to rehydrate via the
  ``step`` ``Literal`` — do NOT grow this into a generic result
  type; misuse becomes a type error at the call site.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InterruptedRunSummary(BaseModel):
    """One-shot summary + drop list produced by ``detect_interrupted_run``.

    Replaces the ``(summary_text, ids_to_drop)`` positional pair
    threaded from ``LifecycleController.detect_interrupted_run`` to
    ``RunController.set_interrupted_summary``. The pair is one
    semantic value ("what to tell the agent + which pending rows
    to drop after it acknowledges"), so it lives as one type.

    ``summary_text`` is the ``<system-context>`` note the run
    pipeline splices into the next user prompt.
    ``pending_ids_to_drop`` is the list of pending-message row ids
    the ``PendingMessageJournal`` drains once the summary is
    consumed.
    """

    summary_text: str
    pending_ids_to_drop: list[str] = Field(default_factory=list)


class AgnoRunSnapshot(BaseModel):
    """Boundary adapter over Agno's ``session.runs[-1]`` object.

    Agno's per-run objects aren't a stable public schema — the
    fields are dynamic (some versions expose ``tools``, others
    ``tool_calls``; ``content`` may be a str or a list). The
    previous ``detect_interrupted_run`` reached into four such
    fields via ``getattr(..., None)`` at four scattered call
    sites. This adapter absorbs the shape once at the boundary so
    downstream code reads real fields.

    Built via :meth:`from_agno_run` rather than direct validation
    because Agno's run objects aren't guaranteed to be Pydantic-
    compatible types.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str | None = None
    status: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    content_preview: str = ""

    @classmethod
    def from_agno_run(cls, run: Any) -> AgnoRunSnapshot:
        """Extract a typed snapshot from an opaque Agno run object.

        Single site for all the ``getattr(run, X, None)`` probing —
        if Agno's shape shifts across versions, the fix lands here
        rather than in four scattered lines.
        """
        run_id = getattr(run, "run_id", None)
        status_val = getattr(run, "status", None)
        # ``status`` may be an enum; stringify for storage.
        status_str: str | None
        status_str = None if status_val is None else getattr(status_val, "value", str(status_val))

        tool_names: list[str] = []
        for t in getattr(run, "tools", None) or []:
            name = getattr(t, "tool_name", None) or "?"
            tool_names.append(str(name))

        content = getattr(run, "content", None) or ""
        if not isinstance(content, str):
            content = str(content)
        content_preview = content[:400]

        return cls(
            run_id=str(run_id) if run_id is not None else None,
            status=status_str,
            tool_names=tool_names,
            content_preview=content_preview,
        )


class SessionEndPayload(BaseModel):
    """Payload dict for the ``SessionEnd`` hook fire.

    Structurally identical to :class:`StopHookPayload` in
    :mod:`schemas_run` — kept separate on purpose. STOP fires
    after a run naturally ends (per-run event); SESSION_END fires
    when the whole server shuts down (per-session event). Merging
    them would collapse a real semantic distinction the hook
    plugins may key off.
    """

    session_id: str


RehydrateStep = Literal[
    "event_log",
    "orphan_processes",
    "plan_decisions",
    "todos",
    "plan_store",
]


class RehydrateOutcome(BaseModel):
    """Typed per-step result for :class:`RehydrateController` methods.

    Replaces the seven ``try/except → logger.debug → return`` swallows
    that used to hide startup-recovery failures inside the module.
    :meth:`LifecycleController.startup` accumulates the outcomes and
    logs a single summary so ops / tests can pin which step failed.

    * ``ok`` — ``True`` iff the step ran to completion without an
      unhandled exception. A ``no-op`` (nothing to rehydrate) is
      still ``ok=True`` with an empty ``reason``.
    * ``step`` — one of the five rehydrate concerns; misuse is
      caught by the ``Literal`` type.
    * ``reason`` — human-readable failure string on ``ok=False``, or
      a short skip-cause on ``ok=True`` (e.g. ``"no persistence"``,
      ``"already populated"``). Read at INFO in the summary line.
    """

    ok: bool
    step: RehydrateStep
    reason: str | None = None


class ShutdownResult(BaseModel):
    """Outcome of :meth:`LifecycleController.shutdown`.

    Replaces the four bare ``contextlib.suppress(Exception)``
    blocks. Each cleanup step reports whether it succeeded plus
    the human-readable error string on failure. Callers that
    previously treated shutdown as a fire-and-forget still get
    that behaviour (the controller catches per-step) but the
    outcome is now observable — ops / tests / logs can pin which
    step failed instead of guessing at "shutdown seems to hang
    sometimes".

    Behaviour lives on this class rather than on the caller so
    each shutdown step just names the transition it made
    (``result.mark_hook_fired()``) instead of poking the field
    directly — data + operations kept together.
    """

    hook_fired: bool = False
    pool_cleaned: bool = False
    mcp_disconnected: bool = False
    scheduler_stopped: bool = False
    shell_processes_killed: int = 0
    errors: list[str] = Field(default_factory=list)

    def mark_hook_fired(self) -> None:
        """Record that the ``SESSION_END`` hook fired successfully."""
        self.hook_fired = True

    def mark_pool_cleaned(self) -> None:
        """Record that ephemeral pool cleanup completed."""
        self.pool_cleaned = True

    def mark_mcp_disconnected(self) -> None:
        """Record that all MCP clients disconnected."""
        self.mcp_disconnected = True

    def mark_scheduler_stopped(self) -> None:
        """Record that the scheduler background runner stopped."""
        self.scheduler_stopped = True

    def record_shell_kills(self, killed: int) -> None:
        """Record how many background shell processes were killed."""
        self.shell_processes_killed = int(killed)

    def record_error(self, label: str, exc: BaseException) -> None:
        """Append a labelled error string for a failed shutdown step."""
        self.errors.append(f"{label}: {exc}")


class RehydrateOutcomeSet(BaseModel):
    """Bag of :class:`RehydrateOutcome` values from a single startup.

    Owns the summary log ceremony that used to live on
    :class:`LifecycleController` as ``_log_rehydrate_summary``.
    The controller calls ``RehydrateOutcomeSet.of(*outcomes).log(logger)``.
    """

    outcomes: list[RehydrateOutcome] = Field(default_factory=list)

    @classmethod
    def of(cls, *outcomes: object) -> RehydrateOutcomeSet:
        """Ergonomic constructor — filters non-Outcome inputs.

        Production code passes real :class:`RehydrateOutcome` values.
        Test fixtures that bind ``AsyncMock()`` to
        ``BackendServer._rehydrate_*`` (without configuring
        ``return_value``) resolve to ``MagicMock`` / ``None`` at await
        time. Silently dropping those keeps the production type strict
        without forcing every mock to spell out a full outcome payload.
        """
        return cls(outcomes=[o for o in outcomes if isinstance(o, RehydrateOutcome)])

    def log(self, target: logging.Logger) -> None:
        """Emit a single INFO / WARNING summary line for the batch."""
        failed = [o for o in self.outcomes if not o.ok]
        if failed:
            details = ", ".join(f"{o.step}={o.reason}" for o in failed)
            target.warning(
                "Rehydrate: %d/%d step(s) failed — %s",
                len(failed),
                len(self.outcomes),
                details,
            )
        else:
            target.info("Rehydrate: %d step(s) ok", len(self.outcomes))
