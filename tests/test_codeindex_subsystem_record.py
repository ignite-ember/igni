"""CodeIndex publishes its state to the subsystem registry.

The second customer, and the reason the registry is a registry rather
than a couple of fields on a session: every optional part of this app
needs somewhere to say what it is doing and why, and each one that
invents its own vocabulary invents a worse version of the same four
states. Knowledge had three ad-hoc ones; CodeIndex had "not indexed"
covering everything from "still syncing" to "you are not logged in".
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ember_code.backend.server_codeindex import CodeIndexController, _InstallState
from ember_code.backend.subsystem_status import CODE_INDEX, SubsystemRegistry, SubsystemState


def _controller() -> tuple[CodeIndexController, SubsystemRegistry]:
    controller = CodeIndexController.__new__(CodeIndexController)
    session = MagicMock()
    registry = SubsystemRegistry()
    session.subsystems = registry
    controller._session = session
    return controller, registry


def _record(controller, registry, install, **kwargs):
    defaults = {"head_indexed": False, "syncing": False, "sync_error": ""}
    defaults.update(kwargs)
    controller._record_subsystem(install, **defaults)
    return registry.get(CODE_INDEX)


def test_an_unresolved_install_records_the_reason_not_a_shrug():
    """The bug in one assertion: "not logged in" is a disabled
    subsystem with a remedy, not an index that needs syncing."""
    controller, registry = _controller()
    install = _InstallState(
        "unknown", "", "", reason="not logged in to igni Cloud", fix="Run /login."
    )

    entry = _record(controller, registry, install)

    assert entry.state is SubsystemState.DISABLED
    assert "not logged in" in entry.reason
    assert entry.fix == "Run /login."


def test_an_indexed_head_is_ready():
    controller, registry = _controller()

    entry = _record(controller, registry, _InstallState("installed", "r1", ""), head_indexed=True)

    assert entry.state is SubsystemState.READY


def test_syncing_is_preparing_not_failed():
    controller, registry = _controller()

    entry = _record(controller, registry, _InstallState("installed", "r1", ""), syncing=True)

    assert entry.state is SubsystemState.PREPARING


def test_an_unindexed_head_is_preparing_too():
    """ "Not indexed yet" on a connected repo is a legitimate
    in-between, and the only one of these four that the old wording
    actually described."""
    controller, registry = _controller()

    entry = _record(controller, registry, _InstallState("installed", "r1", ""))

    assert entry.state is SubsystemState.PREPARING
    assert "not indexed" in entry.reason


def test_a_sync_error_is_a_failure_with_a_remedy():
    controller, registry = _controller()

    entry = _record(
        controller,
        registry,
        _InstallState("installed", "r1", ""),
        sync_error="apply failed at commit abc123",
    )

    assert entry.state is SubsystemState.FAILED
    assert "abc123" in entry.reason
    assert "resync" in entry.fix


def test_a_repo_the_app_cannot_see_says_where_to_connect_it():
    controller, registry = _controller()

    entry = _record(controller, registry, _InstallState("needs_install", "", "https://x/install"))

    assert entry.state is SubsystemState.DISABLED
    assert "not connected" in entry.reason
    assert "portal" in entry.fix


def test_a_session_without_a_registry_is_not_an_error():
    """Sessions built outside the backend's path have none, and a
    status poll that failed over bookkeeping would take the footer
    pill down with it."""
    controller = CodeIndexController.__new__(CodeIndexController)
    session = MagicMock()
    session.subsystems = None
    controller._session = session

    controller._record_subsystem(
        _InstallState("installed", "r1", ""), head_indexed=True, syncing=False, sync_error=""
    )
