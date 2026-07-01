"""Plan mode — Claude Code parity (row 50).

A user-toggled mode where the agent can read / search / think but
**cannot** write files or run mutating shell commands. The
enforcement lives in :class:`PermissionEvaluator` (already
implemented as ``PermissionMode.PLAN`` with row 7); this module
adds the missing pieces:

* :class:`PlanStore` — per-session capture of the latest plan
  the agent submitted via ``exit_plan_mode``. Surfaced to the UI
  via the ``GET_LATEST_PLAN`` RPC.
* :class:`PlanTool` — registers ``exit_plan_mode(plan)`` so the
  agent can signal "I'm done planning" at the end of a plan-mode
  turn. The tool does NOT flip the mode out of plan automatically
  — the user controls that via ``/plan`` so the agent can't
  exit the sandbox on its own.

The complementary half (``/plan`` slash command +
``Session.set_permission_mode``) lives in ``backend/command_handler.py``
and ``core/session/core.py`` respectively.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


# Validation thresholds. Tuned so a thin "trust me bro" plan
# bounces but a half-decent grounded plan goes through. The
# attempts cap exists to break the loop when the agent
# genuinely can't satisfy us — better to surface a thin plan to
# the user (who can refine it) than to ping-pong forever.
_MIN_FILE_CITATIONS = 2
_MAX_PLAN_ATTEMPTS = 3
# Regex catches typical file-path shapes: ``foo/bar.py``,
# ``src/x.ts``, ``backticked/code/files.md``. Generous on the
# extension list — we want to count file mentions in plan
# markdown across mixed-language repos.
_FILE_PATH_RE = re.compile(
    r"[\w./_-]+\.(?:py|ts|tsx|js|jsx|md|rs|go|java|kt|swift|c|cc|cpp|h|hpp|"
    r"rb|php|cs|scala|css|scss|sql|sh|toml|yaml|yml|json|html|jinja)"
)


@dataclass
class _ConfidenceVerdict:
    """Result of ``_validate_plan_confidence``. The reply path
    returns ``feedback`` to the agent verbatim on rejection."""

    reject: bool
    feedback: str
    attempts_remaining: int


def _validate_plan_confidence(
    plan: str,
    tasks: list | None,
    session: Session | object,
) -> _ConfidenceVerdict:
    """Enforce the "plan must be grounded" property.

    Currently checks one signal: the plan markdown must mention
    at least ``_MIN_FILE_CITATIONS`` file paths. Plans without
    them are usually hand-wavy and produce execution-time
    surprises ("oh, I'll find the right file later"). The check
    runs only when ``_codeindex_available`` — without an index
    the agent legitimately can't do as deep a research pass, so
    we skip the gate.

    Bounded by ``_MAX_PLAN_ATTEMPTS`` via the
    ``_plan_mode_attempt`` counter on the session — after the
    cap we accept whatever came in (the user still reviews +
    can refine).
    """
    # If the index isn't available, skip the gate entirely.
    # Otherwise we'd be enforcing a research methodology the
    # agent doesn't have the tools to satisfy. ``is True``
    # (strict) defends against MagicMock leakage in tests —
    # production code sets a real bool here.
    if getattr(session, "_codeindex_available", False) is not True:
        return _ConfidenceVerdict(reject=False, feedback="", attempts_remaining=0)

    attempt = int(getattr(session, "_plan_mode_attempt", 0))
    remaining = max(0, _MAX_PLAN_ATTEMPTS - attempt - 1)
    if attempt >= _MAX_PLAN_ATTEMPTS - 1:
        # On the LAST allowed attempt we accept whatever came in
        # — surfacing a thin plan beats infinite loops. The
        # counter resets when the next ``enter_plan_mode`` fires.
        return _ConfidenceVerdict(reject=False, feedback="", attempts_remaining=0)

    citations = set(_FILE_PATH_RE.findall(plan or ""))
    task_count = len(tasks or [])
    needs_more = len(citations) < _MIN_FILE_CITATIONS

    if not needs_more:
        return _ConfidenceVerdict(reject=False, feedback="", attempts_remaining=remaining)

    # Bump the attempt counter so the next call sees a different
    # state.
    if hasattr(session, "_plan_mode_attempt"):
        session._plan_mode_attempt = attempt + 1

    feedback = (
        f"Plan rejected (research pass {attempt + 1}/{_MAX_PLAN_ATTEMPTS}). "
        f"Found only {len(citations)} file citation(s); need at least "
        f"{_MIN_FILE_CITATIONS}. Your plan must reference specific files "
        "you actually examined — symbol-only descriptions don't ground "
        "the plan enough.\n\n"
        "Do another research pass:\n"
        "1. Run `codeindex_query` with at least 2 distinct angles you "
        "haven't tried yet (e.g. symbol names, related features, file "
        "patterns).\n"
        "2. For the top hits, call `codeindex_tree(id=<uuid>)` to see "
        "the reference graph — that finds the blast radius of any "
        "change.\n"
        "3. `file_read` one or two of the most central files to see "
        "exact source.\n"
        "4. Submit `exit_plan_mode(plan, tasks=[...])` again with "
        f"specific file paths cited. {task_count} task(s) currently."
    )
    return _ConfidenceVerdict(reject=True, feedback=feedback, attempts_remaining=remaining)


_VALID_DECISIONS = ("approved", "dismissed")


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
    # ``run_id`` -> ``"approved"`` | ``"dismissed"``. Absent
    # key means the user hasn't acted yet (pending). The
    # mapping is the SOLE source of truth for plan state —
    # never inferred from mode, never from message content.
    decisions: dict[str, str] = field(default_factory=dict)
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

    def set_decision(self, run_id: str, decision: str) -> None:
        """Record the user's decision for a specific plan
        (identified by the ``run_id`` of the run in which
        ``exit_plan_mode`` was called).

        ``decision`` must be ``"approved"`` or ``"dismissed"`` —
        anything else raises so a typo in calling code surfaces
        immediately instead of silently corrupting the store.
        ``run_id`` must be a non-empty string for the same
        reason (an empty key would collide across plans).
        """
        if decision not in _VALID_DECISIONS:
            raise ValueError(f"decision must be one of {_VALID_DECISIONS}, got {decision!r}")
        if not run_id:
            raise ValueError("run_id must be non-empty")
        self.decisions[run_id] = decision

    def get_decision(self, run_id: str) -> str | None:
        """Return the recorded decision or ``None`` if the user
        hasn't acted on this plan yet."""
        if not run_id:
            return None
        return self.decisions.get(run_id)

    def load_decisions(self, data: dict | None) -> None:
        """Bulk-load decisions from the persisted blob. Tolerates
        ``None`` / wrong-shaped values — anything that doesn't
        look like a ``str -> valid-decision`` mapping is
        dropped silently. Called on session rehydrate."""
        if not isinstance(data, dict):
            return
        for run_id, decision in data.items():
            if isinstance(run_id, str) and decision in _VALID_DECISIONS:
                self.decisions[run_id] = decision

    def decisions_snapshot(self) -> dict[str, str]:
        """Copy of the decisions map for persistence. Returning
        a copy (not the live dict) keeps the persistence layer
        from accidentally mutating store state when it
        serializes."""
        return dict(self.decisions)

    def snapshot(self) -> dict:
        """Wire shape for the panel / `get_latest_plan` RPC."""
        return {"latest": self.latest, "history": list(self.history)}


class PlanTool(Toolkit):
    """Registers ``enter_plan_mode`` + ``exit_plan_mode`` on the
    agent's tool list.

    Asymmetric security envelope:

    * ``enter_plan_mode`` flips mode → ``plan``. Safe to expose to
      the agent — moving INTO the sandbox is strictly stricter
      (read-only). The agent self-disciplines for complex tasks.
    * ``exit_plan_mode`` records a plan but does NOT touch the
      mode. Exit is user-controlled (``/plan off`` or the Approve
      button) so the agent can't unsandbox itself.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_plan")
        self._session = session
        self.register(self.enter_plan_mode)
        self.register(self.exit_plan_mode)

    async def enter_plan_mode(self, reason: str = "", task: str = "") -> str:
        """Enter plan mode AND spawn the plan_researcher sub-agent.

        Call this BEFORE doing any work when the user asks for
        something that benefits from a written plan:

        * Multi-file refactors / architectural changes
        * "Add feature X" requests that span several modules
        * Investigations where the right path isn't obvious
        * Anything where committing to a direction without
          checking with the user first would be expensive to
          undo

        Don't call this for simple, one-shot requests
        (a small bug fix, a single-file edit, an obvious tweak).

        Args:
            reason: Short string the UI shows next to the plan-mode
                badge ("auth refactor spans 4 services"). Helps
                the user understand WHY you switched modes.
            task: The user's original request, verbatim or
                paraphrased. Passed to the spawned
                ``plan_researcher`` sub-agent so it knows what
                to research. When provided, this method
                automatically spawns the researcher and returns
                its findings — you don't need a separate
                ``spawn_agent`` call. Omit only for very short
                "I want to plan this manually" turns.

        Behavior:
        1. Flips the permission evaluator to ``plan`` (blocks
           file edits + mutating shell).
        2. Resets the plan-mode attempt counter.
        3. If ``task`` is provided, spawns the
           ``plan_researcher`` sub-agent with the task. The
           researcher does multi-angle CodeIndex queries (or
           grep fallback), reads the critical files, and
           returns a structured report (Findings / Proposed
           Plan / Tasks JSON / Confidence / Open Questions).
        4. Returns the researcher's report so you can use it
           verbatim or refine it before calling
           ``exit_plan_mode(plan, tasks)``.

        The validation hook in ``exit_plan_mode`` may reject
        plans that aren't grounded in concrete codebase facts —
        if so, the rejection message tells you exactly what's
        missing, do another research pass, then submit again.
        Bounded by 3 attempts.
        """
        if not hasattr(self._session, "set_permission_mode"):
            return "Error: session does not support runtime mode changes."
        reason_clean = (reason or "").strip()
        task_clean = (task or "").strip()
        status = self._session.set_permission_mode("plan")
        if "Error" in status:
            return status
        # Reset the per-plan-mode-session validation state so the
        # iteration counter starts fresh on every fresh entry.
        if hasattr(self._session, "_plan_mode_attempt"):
            self._session._plan_mode_attempt = 0

        # Re-broadcast with the agent attribution + reason so the
        # FE can render a small info banner.
        if hasattr(self._session, "broadcast"):
            self._session.broadcast(
                "permission_mode_changed",
                {
                    "mode": "plan",
                    "source": "agent",
                    "reason": reason_clean,
                },
            )

        # Spawn the plan_researcher sub-agent on the task. The
        # researcher operates in plan mode itself (same evaluator
        # — file edits blocked) and produces a structured report
        # the caller (main agent) turns into the final
        # ``exit_plan_mode`` call.
        researcher_report = ""
        if task_clean:
            researcher_report = await self._run_plan_researcher(task_clean)

        tail = f" ({reason_clean})" if reason_clean else ""
        header = f"Entered plan mode{tail}. File edits and mutating shell commands are now blocked."
        if researcher_report:
            return (
                f"{header}\n\nplan_researcher sub-agent report follows. "
                "Review it, refine if needed, then call "
                "exit_plan_mode(plan, tasks=[...]) with the final plan. "
                "If the report is thin, call enter_plan_mode again with "
                "a sharper task description for another research pass.\n\n"
                "---\n\n"
                f"{researcher_report}"
            )
        return (
            f"{header} Gather context (CodeIndex queries first, then file_read), "
            "then call exit_plan_mode(plan, tasks=[...]) with a concrete proposal. "
            "Tip: pass the user's request as `task=` next time and this tool will "
            "spawn the plan_researcher sub-agent for you automatically."
        )

    async def _run_plan_researcher(self, task: str) -> str:
        """Internal: spawn the ``plan_researcher`` sub-agent on
        ``task`` and return its response text. Returns ``""`` on
        any failure (no researcher available, spawn error, etc.)
        so ``enter_plan_mode`` can fall back to manual research."""
        from ember_code.core.tools.orchestrate import OrchestrateTools

        # Find the OrchestrateTools instance already wired onto
        # the session's main agent. We reuse it instead of
        # constructing a fresh one because it carries the
        # progress callback, HITL coordinator, and depth counter
        # already plumbed by the session.
        orchestrate: OrchestrateTools | None = None
        team_tools = getattr(getattr(self._session, "main_team", None), "tools", None) or []
        for tool in team_tools:
            if isinstance(tool, OrchestrateTools):
                orchestrate = tool
                break
        if orchestrate is None:
            logger.debug("plan_researcher spawn skipped — no OrchestrateTools on session")
            return ""

        # Check the agent is registered. The pool's
        # ``_codeindex_available`` flag picks the right variant
        # (``plan_researcher.codeindex.md`` vs ``plan_researcher.md``)
        # — both register under the same canonical name.
        try:
            orchestrate.pool.get("plan_researcher")
        except KeyError:
            logger.debug("plan_researcher agent definition not found in pool")
            return ""

        try:
            result = await orchestrate.spawn_agent(task=task, agent_name="plan_researcher")
        except Exception as exc:
            logger.warning("plan_researcher spawn failed: %s", exc)
            return ""
        return result if isinstance(result, str) else str(result)

    def exit_plan_mode(self, plan: str, tasks: list | None = None) -> str:
        """Submit a plan for the user's review.

        Call this at the END of a plan-mode turn after you've
        finished gathering context and have a concrete proposal
        for what to do next.

        Args:
            plan: Markdown-formatted plan describing the steps
                you intend to take. This is the prose the user
                reads when deciding whether to approve.
            tasks: Optional list of structured tasks — one entry
                per execution step. Each entry is a dict with
                ``content`` (required, the imperative step
                description) and optional ``activeForm`` (the
                verb-noun gerund shown while in progress). The
                tool populates the session's ``TodoStore`` with
                these so the user sees a checklist alongside
                the prose plan, AND the same store updates live
                as you call ``todo_write`` during execution.
                Pass tasks unless the plan is genuinely
                unstructured (a freeform proposal where steps
                aren't enumerable).

        Example call::

            exit_plan_mode(
                plan="## JWT refactor\\n\\nMove from session cookies to JWT...",
                tasks=[
                    {"content": "Generate JWT signing keys",
                     "activeForm": "Generating JWT signing keys"},
                    {"content": "Add /auth/refresh endpoint",
                     "activeForm": "Adding /auth/refresh endpoint"},
                    {"content": "Migrate session table",
                     "activeForm": "Migrating session table"},
                ],
            )

        Plan-mode etiquette:

        * Only call this when you're in plan mode (the user
          enabled it via ``/plan`` or you called
          ``enter_plan_mode``). Calling it outside plan mode
          still records the plan, but it's noise — nobody asked
          for one.
        * Stop after calling this tool. Do NOT continue
          executing steps in the same turn — the whole point of
          plan mode is that the user reviews before execution.
          Wait for their next message.

        Returns a confirmation. The plan + tasks are stored on
        the session for the UI to render and the user can
        ``/plan`` (or click Approve) to exit plan mode and let
        you execute.
        """
        plan_text = (plan or "").strip()
        if not plan_text:
            return "Error: plan is empty. Pass a markdown-formatted plan describing what you intend to do."
        store = getattr(self._session, "plan_store", None)
        if store is None:
            return "Error: plan store not initialised on this session."

        # ── Confidence check (row 50 enforcement) ─────────────
        # Reject submissions that aren't grounded in concrete
        # codebase facts. The rejection is bounded by an attempt
        # counter so the loop converges even when the model
        # can't satisfy us; after the cap we accept whatever
        # came in (the user still reviews + can refine).
        verdict = _validate_plan_confidence(plan_text, tasks, self._session)
        if verdict.reject and verdict.attempts_remaining > 0:
            return verdict.feedback

        store.set_plan(plan_text)

        # Structured tasks → TodoStore so the PlanCard can render
        # a live checklist alongside the prose plan. Reuse
        # ``_coerce_items`` so the validation rules (status enum,
        # required content, activeForm aliasing) stay in lockstep
        # with ``todo_write``.
        task_snapshot: list = []
        validation_errors: list[str] = []
        if tasks:
            from ember_code.core.tools.todo import _coerce_items

            items, errs = _coerce_items(tasks)
            validation_errors = errs
            if items and hasattr(self._session, "todo_store"):
                self._session.todo_store.set(items)
                task_snapshot = self._session.todo_store.snapshot()

        # Broadcast so the FE can render the plan card inline +
        # show the approve/reject buttons. Best-effort — sessions
        # without a wired transport (headless, tests) silently
        # skip via the empty broadcast list. ``tasks`` rides
        # along in the payload so the FE seeds the PlanCard's
        # checklist on first render (later ``todos_updated``
        # pushes refresh statuses live).
        #
        # Defer to AFTER the run finishes: the PlanCard is the
        # outcome of the run, so it should land below the agent's
        # closing reply in the chat list, not mid-stream above it.
        # ``queue_post_run_broadcast`` falls back to immediate
        # ``broadcast`` for headless callers / tests that built the
        # session without the queue.
        payload = {"plan": plan_text, "tasks": task_snapshot}
        if hasattr(self._session, "queue_post_run_broadcast"):
            self._session.queue_post_run_broadcast("plan_submitted", payload)
        elif hasattr(self._session, "broadcast"):
            self._session.broadcast("plan_submitted", payload)

        reply = (
            "Plan submitted. Stop here — the user will review and either "
            "exit plan mode via `/plan` (to let you execute) or ask for "
            "changes. Do not continue executing in this turn."
        )
        if task_snapshot:
            reply += (
                f" ({len(task_snapshot)} structured task(s) populated; "
                "they'll tick off as you call todo_write during execution)."
            )
        if validation_errors:
            reply += f" Tasks validation errors (ignored): {'; '.join(validation_errors)}"
        return reply
