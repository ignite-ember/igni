"""Restart-policy evaluator and per-monitor supervisor loop.

The old ``MonitorManager._supervise`` free-method-with-a-state-arg
splits into two collaborators:

* :class:`RestartPolicyEvaluator` — pure policy: given a
  :class:`RestartPolicy`, an exit code, and a crash count, return
  a typed :class:`RestartDecision`. No I/O, no mutation.
* :class:`MonitorSupervisor` — the state machine: subscribes to
  a single :class:`MonitorHandle`'s exits and applies the
  evaluator's decisions via the handle's public methods.

The supervisor never touches the handle's private attributes; if
you find yourself wanting to, add a method to the handle instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ember_code.core.monitors.config import RestartPolicy
from ember_code.core.monitors.handle import MonitorHandle
from ember_code.core.monitors.models import (
    BackoffDecision,
    GiveUpDecision,
    RestartDecision,
    StopDecision,
)

logger = logging.getLogger(__name__)


# Restart backoff schedule (seconds). After ``len(schedule)``
# consecutive crashes the evaluator returns a
# :class:`GiveUpDecision` — the agent / user can then call
# ``restart`` to reset the counter.
_DEFAULT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 5.0, 15.0)


@dataclass(frozen=True)
class RestartPolicyEvaluator:
    """Pure decision function over the restart policy.

    Kept as a ``@dataclass(frozen=True)`` instead of a Pydantic
    model because it holds a single tuple and is called in the
    supervisor's hot loop — the extra Pydantic validation per
    ``.decide()`` call is not worth the polymorphism-shaped
    ergonomics here.
    """

    backoff_schedule: tuple[float, ...] = field(default=_DEFAULT_BACKOFF)

    @property
    def max_attempts(self) -> int:
        return len(self.backoff_schedule)

    def decide(
        self,
        policy: RestartPolicy,
        exit_code: int | None,
        crash_count: int,
    ) -> RestartDecision:
        """Return the action the supervisor should take next.

        ``crash_count`` is the pre-bump count (i.e. how many times
        we've already restarted). The evaluator returns a
        :class:`BackoffDecision` with the ``attempt`` field set to
        the *new* attempt number (1-indexed) so the supervisor can
        log "restart attempt 3 of 4".
        """
        if policy == "never":
            return StopDecision(reason="policy=never")
        if policy == "on_crash" and (exit_code == 0 or exit_code is None):
            return StopDecision(reason="clean exit under on_crash policy")
        next_attempt = crash_count + 1
        if next_attempt > self.max_attempts:
            return GiveUpDecision(
                reason=(f"[monitor exceeded {self.max_attempts} restart attempts — giving up]")
            )
        delay = self.backoff_schedule[next_attempt - 1]
        return BackoffDecision(delay_seconds=delay, attempt=next_attempt)


class MonitorSupervisor:
    """Watches a single monitor for exits and restarts it per
    policy. Owns *no* state itself — everything lives on the
    :class:`MonitorHandle`."""

    def __init__(
        self,
        handle: MonitorHandle,
        evaluator: RestartPolicyEvaluator | None = None,
    ) -> None:
        self._handle = handle
        self._evaluator = evaluator or RestartPolicyEvaluator()

    async def run(self) -> None:
        """Main supervisor loop. Was ``MonitorManager._supervise``.

        Every mutation of the handle goes through the handle's
        public methods — this loop is a state machine over typed
        :class:`RestartDecision` values, no reach-in.
        """
        handle = self._handle
        try:
            while True:
                code = await handle.wait_exit()
                handle.note_exit(code)
                if handle.is_stopping():
                    handle.mark_stopped(exit_code=code)
                    return
                decision = self._evaluator.decide(
                    policy=handle.config.restart,
                    exit_code=code,
                    crash_count=handle.crash_count,
                )
                if isinstance(decision, StopDecision):
                    handle.mark_stopped(exit_code=code)
                    return
                if isinstance(decision, GiveUpDecision):
                    handle.mark_failed(decision.reason)
                    return
                # BackoffDecision — bump the counter, log, sleep,
                # relaunch. If MonitorSnapshot's decision union
                # ever grows past ~5 variants, replace this
                # isinstance ladder with a ``.apply(handle)``
                # method on each decision class.
                handle.bump_crash_count()
                handle.append_output(
                    f"[monitor exited (code={code}); restarting in {decision.delay_seconds:.0f}s]"
                )
                await asyncio.sleep(decision.delay_seconds)
                if handle.is_stopping():
                    return
                await handle.relaunch_under_lock()
                if handle.status.value != "running":
                    return
        except asyncio.CancelledError:
            raise


__all__ = ["MonitorSupervisor", "RestartPolicyEvaluator"]
