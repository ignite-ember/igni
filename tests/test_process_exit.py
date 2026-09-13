"""Exit codes are turned into sentences.

``137`` cost an hour. It is ``128 + 9`` — SIGKILL — and the JDK behind
it had been left half-extracted, so every JVM the backend started died
instantly, wrote nothing, and reported a number that looks like an
ordinary failure.
"""

from __future__ import annotations

import signal

from ember_code.backend.process_exit import describe_exit, format_child_failure


def test_the_one_that_cost_an_hour():
    described = describe_exit(137)

    assert "SIGKILL" in described
    assert "137" in described
    # The hint matters more than the name: "killed by SIGKILL" still
    # leaves a reader wondering who did the killing.
    assert "binary" in described or "memory" in described


def test_asyncio_negative_convention():
    """``asyncio`` reports a signal death as a negative number; a shell
    reports ``128 + signal``. Both reach us, so both are read."""
    assert "SIGKILL" in describe_exit(-9)
    assert "SIGKILL" in describe_exit(137)


def test_ordinary_failures_are_not_dressed_up_as_signals():
    assert describe_exit(1) == "exited with status 1"
    assert describe_exit(2) == "exited with status 2"


def test_success_and_still_running():
    assert "cleanly" in describe_exit(0)
    assert describe_exit(None) == "still running"


def test_a_signal_with_no_hint_still_names_itself():
    described = describe_exit(128 + int(signal.SIGHUP))

    assert "SIGHUP" in described


def test_an_unknown_signal_number_does_not_raise():
    """Guard against a platform reporting something outside the enum —
    a crash in the error path is a special kind of unhelpful."""
    described = describe_exit(128 + signal.NSIG - 1)

    assert "signal" in described


def test_silence_is_reported_as_a_finding():
    """A dead child that wrote nothing is a fact worth stating. Leaving
    it blank reads as "the log got lost", which sends the next person
    looking in the wrong place — I know, because it sent me."""
    line = format_child_failure("neo4j", 137, "")

    assert "wrote no output" in line


def test_the_tail_is_what_gets_kept():
    """A process that dies during startup says the useful part last."""
    line = format_child_failure("neo4j", 1, "early noise\n" + "x" * 5000 + "\nthe actual error")

    assert "the actual error" in line
    assert "early noise" not in line
    assert len(line) < 2500


def test_short_output_is_kept_whole():
    line = format_child_failure("neo4j", 1, "Address already in use")

    assert "Address already in use" in line
    assert "…" not in line
