"""A subsystem may switch itself off. It may not do so anonymously.

The registry's one rule — a failure carries a reason — is the whole
point of the module. Everything else is a dict.
"""

from __future__ import annotations

import pytest

from ember_code.backend.subsystem_status import (
    CODE_INDEX,
    KNOWLEDGE,
    SubsystemRegistry,
    SubsystemState,
)


def test_a_failure_without_a_reason_is_refused():
    """The exact bug this module exists to prevent: a subsystem that is
    off, and nothing anywhere saying why. Refused at the point of
    writing rather than discovered later by someone staring at an empty
    panel."""
    registry = SubsystemRegistry()

    with pytest.raises(ValueError, match="must say why"):
        registry.set(KNOWLEDGE, SubsystemState.FAILED)


def test_the_other_states_need_no_reason():
    """ "Ready" explains itself."""
    registry = SubsystemRegistry()

    registry.set(KNOWLEDGE, SubsystemState.READY)

    assert registry.get(KNOWLEDGE).working is True


def test_preparing_is_not_failed():
    """The distinction that makes a first launch legible: minutes of
    downloading is not an error, and calling it one sends people to fix
    a working system."""
    registry = SubsystemRegistry()

    registry.set(KNOWLEDGE, SubsystemState.PREPARING, reason="downloading the local database")
    entry = registry.get(KNOWLEDGE)

    assert entry.state is SubsystemState.PREPARING
    assert entry.working is False


def test_a_reason_and_a_remedy_are_separate_fields():
    """ "The sidecar exited immediately" is not actionable. "Delete
    ~/.ember/neo4j" is. A panel needs both and they are not the same
    sentence."""
    registry = SubsystemRegistry()

    registry.set(
        KNOWLEDGE,
        SubsystemState.FAILED,
        reason="neo4j killed by SIGKILL (signal 9)",
        fix="Delete ~/.ember/neo4j and reopen the app.",
    )
    entry = registry.get(KNOWLEDGE)

    assert entry.reason.startswith("neo4j killed")
    assert entry.fix.startswith("Delete")


def test_a_later_state_replaces_an_earlier_one():
    registry = SubsystemRegistry()

    registry.set(KNOWLEDGE, SubsystemState.PREPARING)
    registry.set(KNOWLEDGE, SubsystemState.READY)

    assert registry.get(KNOWLEDGE).state is SubsystemState.READY


def test_an_unknown_subsystem_reads_as_none_not_as_an_error():
    """A panel asking about something never recorded should fall back,
    not raise — sessions built outside the backend's attach path have
    nothing in here."""
    assert SubsystemRegistry().get(KNOWLEDGE) is None


def test_listing_is_stable():
    """A diagnostics view wants the same order every render."""
    registry = SubsystemRegistry()
    registry.set(KNOWLEDGE, SubsystemState.READY)
    registry.set(CODE_INDEX, SubsystemState.DISABLED, reason="not indexed yet")

    assert [s.name for s in registry.all()] == [CODE_INDEX, KNOWLEDGE]


def test_the_wire_shape_carries_all_four_fields():
    registry = SubsystemRegistry()
    registry.set(KNOWLEDGE, SubsystemState.FAILED, reason="boom", fix="try this")

    assert registry.to_wire() == [
        {"name": KNOWLEDGE, "state": "failed", "reason": "boom", "fix": "try this"}
    ]
