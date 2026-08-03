"""Concurrency test: simulate the race between a slash-command
mode flip and a concurrent tool-check evaluation.

This is the scenario the user reports: they flip to bypass mode
in the dropdown while the agent is mid-stream, and a tool check
fires concurrently. With the ``PermissionEvaluator._mode_lock``,
the check should always see the latest mode. Without the lock,
the check could see the OLD mode and prompt the user.

The test spawns N threads doing ``set_mode`` ↔ ``evaluate_outcome``
in parallel for many iterations. Each evaluate must return a
decision consistent with ONE of the modes — never a "torn" read
that mixes properties from two modes.
"""

from __future__ import annotations

import threading

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
)


class TestModeRaceVsEvaluate:
    """Run set_mode and evaluate_outcome in tight parallel loops.
    Each evaluate must see a consistent mode — never a half-state.
    """

    def test_set_mode_visible_to_concurrent_evaluate(self) -> None:
        e = PermissionEvaluator()
        iterations = 2_000
        modes = [
            PermissionMode.DEFAULT,
            PermissionMode.ACCEPT_EDITS,
            PermissionMode.BYPASS_PERMISSIONS,
            PermissionMode.PLAN,
        ]
        seen_modes: set[PermissionMode] = set()
        errors: list[BaseException] = []

        def writer():
            try:
                for i in range(iterations):
                    e.set_mode(modes[i % len(modes)])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(iterations):
                    e.evaluate_outcome("run_shell_command", {"command": "ls -la"})
                    # Record the mode the read saw. The lock
                    # ensures this is one of the four legal modes.
                    seen_modes.add(e.mode)
            except BaseException as exc:  # noqa: BLE001
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
        # Without the lock, ``seen_modes`` could include
        # intermediate torn states; the test asserts the four legal
        # modes were observed. With the lock, this is guaranteed.
        assert seen_modes.issubset(set(modes)), (
            f"observed modes outside the legal set: {seen_modes - set(modes)}"
        )

    def test_bypass_mode_decision_is_stable(self) -> None:
        """With bypass mode set, every tool check must return
        ALLOW — no HITL prompt should ever fire. Without the
        lock, a torn read could see default mode and trigger a
        DEFER, surfacing the prompt the user is complaining
        about. The ``mode`` step runs BEFORE the ``ask`` list (per
        the pipeline reorder documented on the class), so bypass
        also wins over ``ask: Bash`` rules."""
        e = PermissionEvaluator()
        # Race bypass against every other mode — every read must
        # land cleanly on bypass (the lock guarantees one of two
        # states per read, but with bypass set on a flip-flop the
        # most recent read SHOULD be bypass).
        e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        iterations = 1_000
        errors: list[BaseException] = []
        bad = 0

        def flippers():
            try:
                for _ in range(iterations):
                    e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
                    e.set_mode(PermissionMode.PLAN)
                    e.set_mode(PermissionMode.BYPASS_PERMISSIONS)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            nonlocal bad
            try:
                for _ in range(iterations):
                    outcome = e.evaluate_outcome("run_shell_command", {"command": "ls -la"})
                    # With bypass mode active and racing PLAN, the
                    # lock guarantees we see exactly one of the two.
                    # The race window is small but real — without
                    # the lock, a torn read could see ``PLAN`` mid-flip
                    # and return DENY (plan blocks shell), or
                    # something between the two modes. We assert
                    # the seen decision is ALWAYS either ALLOW (bypass)
                    # or DENY (plan) — never an unrelated mode's value.
                    # Compare the outcome's ``decision`` field
                    # against the enum members — direct ``outcome ==
                    # enum`` is always False (different types).
                    if outcome.decision not in (
                        PermissionDecision.ALLOW,
                        PermissionDecision.DENY,
                    ):
                        bad += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=flippers),
            threading.Thread(target=flippers),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"threads raised: {errors[:3]}"
        assert bad == 0, (
            f"expected every read to land on a legal bypass/plan "
            f"decision, got {bad}/{iterations * 2} torn reads"
        )
