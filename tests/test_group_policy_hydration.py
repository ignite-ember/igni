"""Tests for :meth:`AuthController._hydrate_group_policy`.

The controller exposes this method purely so it's reachable from a
unit test — production code reaches it via the cold-start ``__init__``
hook and post-login :meth:`login` hook. Tests construct a minimal
``AuthController`` shell with stubbed collaborators so we can drive
the method directly without booting a full ``BackendServer``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.server_auth import AuthController
from ember_code.core.config.group_policy import GroupPolicyOverrideEntry, GroupPolicyPack


def _make_controller(tmp_path: Path) -> AuthController:
    """Construct the AuthController with minimal stubs.

    We bypass ``__init__``'s cold-start hook (which would otherwise
    schedule an async task) by setting attributes directly. The
    production wiring is exercised in the integration paths and
    covered by the existing group-policy source tests at the
    cache level.
    """

    settings = SimpleNamespace(
        api_url="https://api.example.invalid",
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
    )
    portal = MagicMock()
    portal.fetch_group_pack = AsyncMock()

    # Bypass __init__ — we only want _hydrate_group_policy reachable.
    ctrl = AuthController.__new__(AuthController)
    ctrl._settings = settings
    ctrl._portal = portal
    ctrl._hydration_lock = None  # Lazy-bound on first hydration call.
    return ctrl


def _sample_pack() -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Engineering",
        fetched_at=datetime.now(timezone.utc),
        overrides=[
            GroupPolicyOverrideEntry(
                kind="agents",
                entry_name="reviewer",
                content="---\nname: reviewer\n---\nReview body.",
                content_type="markdown",
                enabled=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_hydrate_fetches_and_materializes(tmp_path: Path):
    ctrl = _make_controller(tmp_path)
    pack = _sample_pack()
    ctrl._portal.fetch_group_pack.return_value = pack

    refreshed = await ctrl._hydrate_group_policy(token="t-1")

    assert refreshed is True
    ctrl._portal.fetch_group_pack.assert_awaited_once_with("t-1")
    # Cache directory should now have the materialized agent file.
    agent_file = tmp_path / "group-policy" / "agents" / "reviewer.md"
    assert agent_file.exists()
    assert agent_file.read_text(encoding="utf-8").startswith("---")


@pytest.mark.asyncio
async def test_hydrate_handles_fetch_failure(tmp_path: Path):
    ctrl = _make_controller(tmp_path)
    ctrl._portal.fetch_group_pack.side_effect = RuntimeError("network down")

    # Must not raise; the controller returns False to signal "no
    # update landed" so callers / audit logs can distinguish from
    # "cache was fresh, no fetch needed".
    refreshed = await ctrl._hydrate_group_policy(token="t-2")
    assert refreshed is False
    assert not (tmp_path / "group-policy").exists()


@pytest.mark.asyncio
async def test_hydrate_handles_none_pack(tmp_path: Path):
    ctrl = _make_controller(tmp_path)
    ctrl._portal.fetch_group_pack.return_value = None

    refreshed = await ctrl._hydrate_group_policy(token="t-3")
    # No update landed (None pack), so the controller reports False.
    assert refreshed is False
    # No files written.
    assert not (tmp_path / "group-policy").exists()


@pytest.mark.asyncio
async def test_hydrate_skipped_when_cache_fresh(tmp_path: Path):
    """A pre-existing fresh cache must not trigger a network call."""
    from ember_code.core.config.group_policy import GroupPolicyCache

    # Pre-populate the cache with a fresh fetch_at
    cache = GroupPolicyCache(cache_dir=tmp_path / "group-policy", data_dir=tmp_path)
    cache.materialize(_sample_pack())

    ctrl = _make_controller(tmp_path)
    refreshed = await ctrl._hydrate_group_policy(token="t-4")
    # The fetch was attempted because we constructed cache directly
    # with ``cache_dir`` not derived from ``data_dir`` — but actually
    # the helper derives cache_dir from data_dir when omitted, so we
    # need to align the test with that. Verify the no-network behavior
    # instead.
    assert refreshed is False
    ctrl._portal.fetch_group_pack.assert_not_called()


@pytest.mark.asyncio
async def test_hydrate_passes_plugin_installer_to_cache(tmp_path: Path):
    """The AuthController must hand the cache a real ``PluginInstaller``
    so ``source_url`` plugins get installed, not skipped-with-warning.
    """
    from ember_code.core.plugins.installer import PluginInstaller

    pack = GroupPolicyPack(
        group_id="g-1",
        group_name="Engineering",
        fetched_at=datetime.now(timezone.utc),
        overrides=[
            GroupPolicyOverrideEntry(
                kind="plugins",
                entry_name="git-plugin",
                content="name: git-plugin\n",
                content_type="yaml",
                enabled=True,
                source_url="https://github.com/example/git-plugin",
                source_ref="main",
                source_subdir=None,
            ),
        ],
    )
    ctrl = _make_controller(tmp_path)
    ctrl._portal.fetch_group_pack.return_value = pack

    # Verify the installer constructor was reached by monkey-patching it
    # and asserting it was called with the controller's data_dir.
    calls: list[dict] = []
    real_init = PluginInstaller.__init__

    def spy_init(self, *args, **kwargs):
        calls.append(kwargs)
        real_init(self, *args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(PluginInstaller, "__init__", spy_init):
        refreshed = await ctrl._hydrate_group_policy(token="t-5")

    assert refreshed is True
    assert calls, "AuthController must instantiate PluginInstaller"
    assert calls[0].get("data_dir") == tmp_path


@pytest.mark.asyncio
async def test_concurrent_hydrations_serialize(tmp_path: Path):
    """Two concurrent ``_hydrate_group_policy`` calls must serialize —
    not double-fetch, not stamp pack_meta twice.
    """

    pack = _sample_pack()
    ctrl = _make_controller(tmp_path)

    fetch_calls = 0

    async def slow_fetch(token: str):
        nonlocal fetch_calls
        fetch_calls += 1
        await asyncio.sleep(0.05)  # Let the second caller queue up
        return pack

    ctrl._portal.fetch_group_pack.side_effect = slow_fetch

    # Fire two hydrations concurrently.
    results = await asyncio.gather(
        ctrl._hydrate_group_policy(token="t-6a"),
        ctrl._hydrate_group_policy(token="t-6b"),
    )

    # One ran, one was blocked on the lock and saw the fresh cache.
    assert results[0] is True and results[1] is False
    # And the wire was hit exactly once.
    assert fetch_calls == 1


@pytest.mark.asyncio
async def test_hydrate_logs_warning_on_failure(tmp_path, monkeypatch):
    """A network failure must log at warning, not debug — so it surfaces
    in the audit log without a debugger attached.

    Why a logger-method patch instead of ``caplog``: earlier tests in
    the full suite import chromadb which reconfigures logging in a
    way that makes ``caplog`` unreliable for our module logger.
    Patching the bound method on the logger instance is immune to
    that — we own the recording surface regardless of global config.
    """
    from ember_code.core.config import group_policy as gp_mod

    messages: list[str] = []
    real_warning = gp_mod.logger.warning

    def _capture(fmt: str, *args, **kwargs) -> None:
        try:
            messages.append(fmt % args if args else fmt)
        except (TypeError, ValueError):
            messages.append(str(fmt))
        real_warning(fmt, *args, **kwargs)

    monkeypatch.setattr(gp_mod.logger, "warning", _capture)

    ctrl = _make_controller(tmp_path)
    ctrl._portal.fetch_group_pack.side_effect = RuntimeError("portal down")
    refreshed = await ctrl._hydrate_group_policy(token="t-fail")

    assert refreshed is False
    assert any("portal down" in m for m in messages), (
        f"fetch failures must surface at warning level; saw {messages!r}"
    )


@pytest.mark.asyncio
async def test_hydrate_logs_info_on_success(tmp_path, monkeypatch):
    """A successful refresh logs the refresh at info level so the audit
    log records when admin pushes propagate. Same logger-patch
    rationale as the warning test (see ``caplog`` caveat there).
    """
    from ember_code.backend import server_auth as auth_mod

    messages: list[str] = []
    real_info = auth_mod.logger.info

    def _capture(fmt: str, *args, **kwargs) -> None:
        try:
            messages.append(fmt % args if args else fmt)
        except (TypeError, ValueError):
            messages.append(str(fmt))
        real_info(fmt, *args, **kwargs)

    monkeypatch.setattr(auth_mod.logger, "info", _capture)

    pack = _sample_pack()
    ctrl = _make_controller(tmp_path)
    ctrl._portal.fetch_group_pack.return_value = pack
    refreshed = await ctrl._hydrate_group_policy(token="t-ok")

    assert refreshed is True
    assert any("refreshed" in m for m in messages), (
        f"success must surface at info level; saw {messages!r}"
    )
