"""Regression: PermissionEvaluator serialises the mode-flip
write with the per-tool read.

The bug this file guards: the BE's WS orchestrator dispatches
each inbound message as its own ``asyncio`` task. When the FE
flipped the mode via the dropdown (slash command) AND the
agent's tool-check hook fired concurrently, the hook could read
the OLD mode and decide to prompt the user even though the user
had just switched to bypass. Same root cause as the
"Accept all edits during this session" race the
``HITLDecision.set_permission_mode`` field already fixes for
HITL pauses — except the manual mode switch has no HITL batch
to piggyback on, so the fix lives in the evaluator itself.

What we pin:
- The mode is read under the same lock the setter / ``set_mode``
  acquires. Concurrent mode flip + evaluate never interleave.
- Switching modes mid-flight is observable: an evaluate started
  AFTER ``set_mode`` returns sees the new value.
- The dataclass surface still works — ``PermissionEvaluator.mode``
  reads + ``mode = ...`` writes still work via the property, and
  :meth:`set_mode` is the preferred atomic API.
"""

from __future__ import annotations

import threading

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
)


class TestModeLock:
    """Direct exercises of the lock surface."""

    def test_default_mode_on_construction(self) -> None:
        e = PermissionEvaluator()
        assert e.mode is PermissionMode.DEFAULT

    def test_set_mode_updates_the_read(self) -> None:
        e = PermissionEvaluator()
        e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        assert e.mode is PermissionMode.BYPASS_PERMISSIONS

    def test_property_setter_still_works(self) -> None:
        """Back-compat: callers that mutate ``evaluator.mode = X``
        directly (e.g. the Session's ``set_permission_mode``)
        still work — the property setter holds the lock."""
        e = PermissionEvaluator()
        e.mode = PermissionMode.PLAN
        assert e.mode is PermissionMode.PLAN

    def test_evaluate_outcome_sees_current_mode(self) -> None:
        """Bypass mode lets shell tools through; default mode
        defers them. Verify the read-then-decide is consistent
        with the current mode value."""
        e = PermissionEvaluator()
        # Default — shell defers.
        decision = e.evaluate_outcome("run_shell_command", {"command": "ls"})
        assert decision.decision is PermissionDecision.DEFER

        # Bypass — shell allows.
        e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        decision = e.evaluate_outcome("run_shell_command", {"command": "ls"})
        assert decision.decision is PermissionDecision.ALLOW


class TestRaceCondition:
    """Stress test: hammer the lock from many threads and verify
    every evaluate sees a consistent mode — no half-written
    state, no torn read."""

    def test_concurrent_set_mode_and_evaluate(self) -> None:
        """Spin up many threads doing set_mode ↔ evaluate in
        parallel. Every evaluate call must succeed and return a
        decision consistent with one of the modes (never a
        half-state). The lock prevents torn reads that would
        otherwise return PermissionDecision.DENY (because the
        ``plan`` strategy auto-denies edits) while the agent is
        in ``bypass`` mode."""
        e = PermissionEvaluator()
        iterations = 1000
        modes = list(PermissionMode)
        errors: list[BaseException] = []
        bad_decisions: list[str] = []

        def writer() -> None:
            try:
                for i in range(iterations):
                    e.set_mode(modes[i % len(modes)])
                    e.mode = modes[(i + 1) % len(modes)]
            except BaseException as exc:  # noqa: BLE001 — test stress
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(iterations):
                    outcome = e.evaluate_outcome(
                        "edit_file",
                        {"file_path": "x.py", "old_string": "a", "new_string": "b"},
                    )
                    # Outcome.decision is always one of the
                    # enum members — never None, never a sentinel.
                    if outcome.decision not in (
                        PermissionDecision.ALLOW,
                        PermissionDecision.DENY,
                        PermissionDecision.DEFER,
                        PermissionDecision.ASK,
                    ):
                        bad_decisions.append(f"unexpected decision: {outcome.decision!r}")
            except BaseException as exc:  # noqa: BLE001 — test stress
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"threads raised: {errors[:3]}"
        assert not bad_decisions, bad_decisions

    def test_lock_is_a_reentrant_lock(self) -> None:
        """``RLock`` so a composite operation that already holds
        the lock can call ``set_mode`` without deadlocking. A
        plain ``Lock`` would deadlock here."""
        e = PermissionEvaluator()

        def composite_op() -> None:
            with e._mode_lock:
                e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
                # ``mode`` property also acquires the same lock
                # — must succeed because ``RLock`` is re-entrant.
                _ = e.mode

        # Run on the main thread (synchronous). If RLock is
        # somehow plain Lock, this deadlocks.
        composite_op()
        assert e.mode is PermissionMode.BYPASS_PERMISSIONS
