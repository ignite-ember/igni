"""``/loop`` state controller for :class:`Session`.

Owns the ``/loop`` in-memory state machine + the two Sqlite-
backed stores (:class:`LoopStore`, :class:`LoopProgressStore`).
The :class:`LoopController` is the SOLE owner of loop state —
:class:`Session` exposes six read-only proxy properties
(``pending_loop_prompt``, ``loop_iteration_index``, …) that
forward to :attr:`LoopController.state` so external callers see
a stable read surface, but the writeable path is methods on
the controller.

The state model:

* :attr:`state.prompt` — active iteration text, or ``None`` /
  empty when no loop is running.
* :attr:`state.run_id` — UUID scoping :class:`LoopProgressStore`
  writes.
* :attr:`state.iteration_index` / :attr:`state.iterations_remaining` —
  1-based counter of iterations dispatched + safety-net budget.
* :attr:`state.cap_explicit` — whether ``max_iter`` is the
  user's intended total (terminate at cap) or a rolling safety
  net (auto-extend at cap, stop only at ``LOOP_HARD_CAP``).
* :attr:`paused` — flipped when an implicit loop hits the
  hard cap, or when an iteration's ``_run`` raises. Cleared by
  :meth:`LoopController.resume_loop` or a fresh
  :meth:`LoopController.start_loop`.
* :attr:`_auto_extended` — one-shot flag consumed by
  :meth:`LoopController.advance_loop` to signal the FE that
  the safety net just rolled over.

Phase transitions go through the private ``_transition`` method
so :attr:`paused` (and hence :attr:`phase`) can never diverge
from :attr:`state`.
"""

from __future__ import annotations

import uuid

from ember_code.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS, LOOP_HARD_CAP
from ember_code.core.loop.prompt import wrap_iteration_prompt
from ember_code.core.loop.store import LoopProgressStore, LoopState, LoopStore
from ember_code.core.loop.tool_results import LoopToolResult
from ember_code.core.session.schemas import LoopAdvance, LoopPhase


class LoopController:
    """State + persistence owner for the ``/loop`` slash command.

    Composes a :class:`LoopStore` (constructor arg) — no host
    reach-in. :attr:`state` is the canonical in-memory record of
    the active loop (``None`` when idle), :attr:`paused` is the
    single writer for the paused flag, and :attr:`phase` is a
    derived read for the lifecycle.

    All mutations go through methods on this class so the
    persisted ``loop_state`` row stays in lockstep with the in-
    memory fields; the state is private — read-only proxy
    accessors on :class:`Session` are the external read surface.
    """

    def __init__(self, loop_store: LoopStore) -> None:
        self._store: LoopStore = loop_store
        self._state: LoopState | None = None
        self._paused: bool = False
        self._auto_extended: bool = False

    # ── State reads (proxied to by Session's read-only properties) ──

    @property
    def state(self) -> LoopState | None:
        """The active loop's persisted-shape state, or ``None``
        when idle. Read-only; callers mutate through the
        ``start_loop`` / ``advance_loop`` / … methods."""
        return self._state

    @property
    def pending_loop_prompt(self) -> str | None:
        """The active iteration text, or ``None`` when idle."""
        return self._state.prompt if self._state is not None else None

    @property
    def loop_iteration_index(self) -> int:
        """1-based counter of iterations dispatched (0 when idle)."""
        return self._state.iteration_index if self._state is not None else 0

    @property
    def loop_iterations_remaining(self) -> int:
        """Safety-net budget remaining (0 when idle)."""
        return self._state.iterations_remaining if self._state is not None else 0

    @property
    def loop_run_id(self) -> str | None:
        """UUID scoping :class:`LoopProgressStore` writes."""
        return self._state.run_id if self._state is not None else None

    @property
    def loop_cap_explicit(self) -> bool:
        """``True`` when the user typed ``/loop N <prompt>``."""
        return self._state.cap_explicit if self._state is not None else False

    @property
    def paused(self) -> bool:
        """Whether the loop is paused (waiting for
        :meth:`resume_loop`)."""
        return self._paused

    @property
    def loop_paused(self) -> bool:
        """Alias of :attr:`paused` — matches the legacy field name
        so :class:`Session`'s ``loop_paused`` proxy has a target
        with the same spelling."""
        return self._paused

    @property
    def loop_store(self) -> LoopStore:
        """The composed :class:`LoopStore` (Sqlite backing).

        Exposed so :class:`Session` can proxy a ``loop_store``
        accessor for callers that expect it on the session, while
        the ownership lives here on the controller.
        """
        return self._store

    @property
    def phase(self) -> LoopPhase:
        """Derived lifecycle phase — single source of truth."""
        if self._state is None:
            return LoopPhase.IDLE
        return LoopPhase.PAUSED if self._paused else LoopPhase.RUNNING

    # ── Phase transitions ─────────────────────────────────────

    def _transition(self, target: LoopPhase) -> None:
        """Move the controller to ``target``, rejecting illegal
        jumps with a clear ``ValueError``.

        Legal transitions (walk the state machine):

        * IDLE → RUNNING (``start_loop``)
        * RUNNING → PAUSED (``pause_loop``, or hard-cap safety pause)
        * PAUSED → RUNNING (``resume_loop`` / ``start_loop`` /
          ``advance_loop`` after resume)
        * RUNNING → IDLE (``cancel_loop`` / explicit cap terminate)
        * PAUSED → IDLE (``cancel_loop``)

        Illegal transitions raise so ``phase`` and the underlying
        fields can never diverge silently.
        """
        current = self.phase
        if target == current:
            return
        legal = {
            LoopPhase.IDLE: {LoopPhase.RUNNING},
            LoopPhase.RUNNING: {LoopPhase.PAUSED, LoopPhase.IDLE},
            LoopPhase.PAUSED: {LoopPhase.RUNNING, LoopPhase.IDLE},
        }
        if target not in legal[current]:
            raise ValueError(f"Illegal /loop transition: {current} → {target}")
        if target == LoopPhase.IDLE:
            self._state = None
            self._paused = False
        elif target == LoopPhase.PAUSED:
            self._paused = True
        else:  # RUNNING
            self._paused = False

    # ── Persistence hydration ─────────────────────────────────

    async def load_persisted_loop_state(self) -> None:
        """Hydrate the in-memory loop fields from the ``loop_state`` row.

        Called by ``BackendServer.startup`` after the session is
        constructed. If the CLI was killed mid-loop, this is what
        restores the prompt + counters so the panel shows the
        interrupted state. The loop is left in the *paused* state —
        no iteration fires until the user explicitly resumes.
        Idempotent — safe to call multiple times.
        """
        state = await self._store.load()
        if state is None:
            return
        # Direct IDLE → PAUSED isn't a legal single transition, so
        # write the state + flip the flag ourselves (mirroring the
        # invariant an IDLE→RUNNING→PAUSED walk would end in).
        self._state = state
        self._paused = True

    # ── Public state-machine methods ──────────────────────────

    async def start_loop(
        self,
        prompt: str,
        max_iter: int,
        *,
        immediate: bool,
        cap_explicit: bool,
    ) -> str:
        """Mint a new ``/loop`` and persist it.

        ``immediate=True`` is the slash-command path: iteration 1
        fires now via the ``run_prompt`` action, so the counters
        start at ``index=1, remaining=max-1``.

        ``immediate=False`` is the agent-tool path: iteration 1
        fires on the *next* idle cycle via :meth:`advance_loop`,
        so counters start at ``index=0, remaining=max``.

        Returns the freshly-minted ``run_id`` so the caller can
        scope :class:`LoopProgressStore` writes to it.
        """
        run_id = str(uuid.uuid4())
        if immediate:
            index = 1
            remaining = max_iter - 1
        else:
            index = 0
            remaining = max_iter
        # Overwrite in one shot: a fresh /loop clears any leftover
        # paused flag from a prior run, so we can't rely on
        # _transition alone (an already-PAUSED phase would refuse
        # the IDLE→RUNNING it needs).
        self._state = LoopState(
            run_id=run_id,
            prompt=prompt,
            iteration_index=index,
            iterations_remaining=remaining,
            cap_explicit=cap_explicit,
        )
        self._paused = False
        await self.persist_loop_state()
        return run_id

    async def advance_loop(self) -> LoopAdvance | None:
        """Pop the next iteration descriptor and persist the new counters.

        Returns ``None`` when no loop is active. See :class:`LoopAdvance`
        for the three effective shapes carried by the returned model.
        """
        if self._state is None:
            return None
        # Paused loops don't auto-advance — see the module docstring.
        if self._paused:
            return None
        state = self._state
        if state.iterations_remaining <= 0:
            # Explicit caps still terminate at the user's N.
            if state.cap_explicit:
                total = state.iteration_index
                await self.cancel_loop()
                return LoopAdvance.completed_at(total)
            # Implicit caps: extend the safety net, or pause at the hard
            # ceiling. Pausing (vs. terminating) at ``LOOP_HARD_CAP``
            # lets the user ``/loop resume`` to keep going past 200.
            if state.iteration_index >= LOOP_HARD_CAP:
                await self.pause_loop()
                return LoopAdvance.safety_paused(state.iteration_index)
            state.iterations_remaining = min(
                LOOP_DEFAULT_MAX_ITERATIONS,
                LOOP_HARD_CAP - state.iteration_index,
            )
            self._auto_extended = True
        state.iterations_remaining -= 1
        state.iteration_index += 1
        await self.persist_loop_state()
        # ``total`` is only meaningful when the user explicitly capped
        # the run; otherwise we send no total to the agent so it
        # doesn't try to pace itself against a fake number.
        cap = state.iteration_index + state.iterations_remaining if state.cap_explicit else None
        wrapped = wrap_iteration_prompt(state.prompt, state.iteration_index, cap)
        # Consume the one-shot ``_auto_extended`` signal — the FE
        # renders an info line when this is True on the next advance
        # so the user knows the safety net just rolled over.
        auto_extended = self._auto_extended
        self._auto_extended = False
        return LoopAdvance.step(
            prompt=wrapped,
            display_prompt=state.prompt,
            iteration=state.iteration_index,
            remaining=state.iterations_remaining,
            # The FE uses this to decide whether the "N remaining after
            # this one" half of the iteration banner is meaningful.
            cap_explicit=state.cap_explicit,
            auto_extended=auto_extended,
        )

    async def cancel_loop(self) -> bool:
        """Clear ``/loop`` state both in memory and on disk.

        Returns whether anything was active before the call — callers
        use this to decide whether to surface a "loop cancelled"
        message vs. silently no-op'ing on a stray cancel. Progress
        rows for the cancelled ``run_id`` are *kept* — the user can
        clear them via the agent tool if they want a clean slate.
        """
        if self._state is None:
            return False
        self._transition(LoopPhase.IDLE)
        await self._store.clear()
        return True

    async def pause_loop(self) -> bool:
        """Flip the active loop to paused without advancing the counter.

        Two callers:

        * :meth:`advance_loop` when an implicit loop hits
          ``LOOP_HARD_CAP`` — instead of terminating, we pause and
          let the user decide via ``/loop resume`` / ``/loop stop``.
        * The FE's ``_check_loop_continuation`` when an iteration's
          ``_run`` raises (429, network, tool failure). Pausing
          without advancing means a subsequent ``/loop resume``
          re-fires the *failed* iteration N.

        Returns False when no loop is active. Idempotent — pausing
        an already-paused loop is a no-op.
        """
        if self._state is None:
            return False
        if self._paused:
            # Already paused: no state change, no persist. Idempotent.
            return True
        self._transition(LoopPhase.PAUSED)
        await self.persist_loop_state()
        return True

    async def resume_loop(self) -> str | None:
        """Unpause an interrupted ``/loop`` and return its prompt.

        Returns the persisted prompt so the caller can fire
        ``_run(prompt)`` and re-run the iteration that was in flight
        when the CLI died. Returns ``None`` when there's no loop to
        resume, or when the loop is already pumping (not paused).
        """
        if self._state is None:
            return None
        if not self._paused:
            return None
        state = self._state
        self._transition(LoopPhase.RUNNING)
        await self.persist_loop_state()
        # Wrap so the resumed iteration carries the same
        # autonomous-loop instructions every other iteration does.
        cap = state.iteration_index + state.iterations_remaining if state.cap_explicit else None
        return wrap_iteration_prompt(state.prompt, state.iteration_index, cap)

    async def persist_loop_state(self) -> None:
        """Write the current loop fields to the ``loop_state`` row.

        Helper called by :meth:`start_loop` / :meth:`advance_loop` /
        :meth:`pause_loop` / :meth:`resume_loop`. :meth:`cancel_loop`
        uses ``loop_store.clear`` directly instead — saving an
        "empty" state would leave a stale row around.

        Public method (previously ``_persist_loop_state``) so
        :class:`Session` can forward without reaching into a
        private controller attribute.
        """
        if self._state is None:
            return
        await self._store.save(self._state)

    # Back-compat alias — external callers (tests, session
    # delegator) may still spell the old private name.
    _persist_loop_state = persist_loop_state

    # ── Tool-facing entry points ──────────────────────────────
    #
    # Each ``*_from_tool`` method owns validation, state check,
    # transition, and typed-result construction for one
    # ``LoopTools`` verb. The tool adapter shrinks to a two-line
    # delegate; the controller stays the single writer for /loop
    # state. Return values are structured — the tool adapter is
    # the only caller that renders them into English via
    # :meth:`LoopToolResult.render`.

    async def start_loop_from_tool(
        self,
        prompt: str,
        max_iterations: int = LOOP_DEFAULT_MAX_ITERATIONS,
    ) -> LoopToolResult:
        """Validate + start a loop from the agent-tool entry point.

        Agent-tool path — iteration 1 fires on the next idle cycle
        via :meth:`advance_loop` (``immediate=False``). The cap is
        always treated as explicit (the agent supplies an intended
        total, not a safety net) so the loop terminates at N
        rather than auto-extending.
        """
        if not prompt or not prompt.strip():
            return LoopToolResult.error_empty_prompt()
        if max_iterations <= 0:
            return LoopToolResult.error_non_positive_max()
        if max_iterations > LOOP_HARD_CAP:
            return LoopToolResult.error_exceeds_hard_cap(max_iterations, LOOP_HARD_CAP)
        if self._state is not None:
            return LoopToolResult.error_already_active(
                iteration_index=self._state.iteration_index,
                iterations_remaining=self._state.iterations_remaining,
            )
        await self.start_loop(
            prompt.strip(),
            max_iterations,
            immediate=False,
            cap_explicit=True,
        )
        return LoopToolResult.armed(max_iterations)

    async def stop_loop_from_tool(self) -> LoopToolResult:
        """Cancel the active loop from the agent-tool entry point.

        Idle short-circuit: reports "nothing to stop" without a
        state mutation. Otherwise snapshots the iteration count
        BEFORE cancelling so the render carries the correct
        completed-iterations number.
        """
        if self._state is None:
            return LoopToolResult.idle_stop()
        iterations_done = self._state.iteration_index
        await self.cancel_loop()
        return LoopToolResult.stopped(iterations_done)

    async def set_announced_total_from_tool(
        self, total: int, progress_store: LoopProgressStore
    ) -> LoopToolResult:
        """Stash the agent-announced iteration total.

        The progress store is a peer of the controller on
        :class:`Session`, so callers thread it through here rather
        than reaching for a store the controller doesn't own.
        """
        if self._state is None or not self._state.run_id:
            return LoopToolResult.error_no_active_loop()
        if total <= 0:
            return LoopToolResult.error_non_positive_total()
        await progress_store.set_announced_total(self._state.run_id, total)
        return LoopToolResult.total_announced(total)

    async def resume_loop_from_tool(self) -> LoopToolResult:
        """Unpause a paused loop from the agent-tool entry point.

        Two "not really an error" no-ops: no loop at all (error),
        and loop is already running (info). Both short-circuit
        before the state mutation.
        """
        if self._state is None:
            return LoopToolResult.error_no_loop_to_resume()
        if not self._paused:
            return LoopToolResult.resume_noop_running()
        prompt = await self.resume_loop()
        if prompt is None:
            # Raced with another writer — the resume path bailed.
            return LoopToolResult.resume_race_no_prompt()
        return LoopToolResult.resumed(self._state.iteration_index)

    def loop_status_from_tool(self) -> LoopToolResult:
        """Report the current lifecycle state — idle vs. active snapshot."""
        if self._state is None:
            return LoopToolResult.status_idle()
        return LoopToolResult.status_active(
            iteration_index=self._state.iteration_index,
            iterations_remaining=self._state.iterations_remaining,
            prompt=self._state.prompt or "",
        )


__all__ = [
    "LoopAdvance",
    "LoopController",
    "LoopPhase",
    "LoopProgressStore",
    "LoopStore",
]
