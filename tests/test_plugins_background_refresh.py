"""Tests for the background marketplace refresh.

Verifies that :meth:`Session.start_marketplace_refresh_background`:

  - Schedules an asyncio task on the running loop.
  - Calls ``refresh_marketplace`` for each registered marketplace.
  - Continues past per-marketplace failures (one bad URL doesn't
    abort the others — failures are log-and-swallow per the design).
  - Is a no-op when there's no running event loop (so calling it
    from a sync context doesn't raise).
  - Is a no-op when no marketplaces are registered.

The Session machinery is heavy, so these tests construct a minimal
session-like object with just the attributes the refresh function
reads (``settings.storage.data_dir``). Real ``load_registry`` /
``refresh_marketplace`` are patched to avoid network access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from ember_code.core.session.core import Session

# Async tests pick up the asyncio mark via pytest-asyncio's auto mode.
# Module-level mark would also (incorrectly) tag
# test_refresh_without_running_loop_is_noop, which is intentionally sync.


# ── Test harness ────────────────────────────────────────────────────


def _make_session_stub(tmp_path: Path) -> Session:
    """A minimal Session object carrying just what
    ``start_marketplace_refresh_background`` reads.

    We bypass ``Session.__init__`` (which would pull in DB, MCP, all
    pools, etc.) and assemble the fields by hand. Cheaper than the
    real fixture and stays focused on the refresh logic."""
    sess = Session.__new__(Session)
    sess.settings = MagicMock()
    sess.settings.storage.data_dir = str(tmp_path / "ember")
    return sess


# ── Behavior ───────────────────────────────────────────────────────


async def test_refresh_calls_refresh_marketplace_for_each_entry(
    tmp_path: Path,
) -> None:
    """The background task iterates every registered marketplace and
    invokes ``refresh_marketplace(name, data_dir=...)`` exactly once
    each. Without this loop the in-session catalog would never
    update past install time."""
    fake_registry = MagicMock()
    e1 = MagicMock()
    e1.name = "m1"
    e2 = MagicMock()
    e2.name = "m2"
    fake_registry.marketplaces = [e1, e2]

    with (
        patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load,
        patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh,
    ):
        mock_load.return_value = fake_registry
        mock_refresh.return_value = MagicMock()
        sess = _make_session_stub(tmp_path)
        sess.start_marketplace_refresh_background()

        # Wait for any spawned tasks (other than this test's task)
        # to finish. The refresh task should call refresh_marketplace
        # twice — once per marketplace.
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert mock_refresh.call_count == 2
    called_names = {c.args[0] for c in mock_refresh.call_args_list}
    assert called_names == {"m1", "m2"}


async def test_refresh_continues_past_failures(tmp_path: Path) -> None:
    """A failing marketplace (raise from ``refresh_marketplace``)
    is logged and swallowed — the next marketplace still gets
    refreshed. Without this, one bad URL in the registry would
    silently break refresh for everything after it."""
    fake_registry = MagicMock()
    good = MagicMock()
    good.name = "good"
    bad = MagicMock()
    bad.name = "bad"
    fake_registry.marketplaces = [bad, good]

    def _side_effect(name, *, data_dir):
        if name == "bad":
            raise RuntimeError("simulated network failure")
        return MagicMock()

    with (
        patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load,
        patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh,
    ):
        mock_load.return_value = fake_registry
        mock_refresh.side_effect = _side_effect
        sess = _make_session_stub(tmp_path)
        sess.start_marketplace_refresh_background()
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # Both were attempted even though the first raised.
    assert mock_refresh.call_count == 2
    names_called = [c.args[0] for c in mock_refresh.call_args_list]
    assert "bad" in names_called and "good" in names_called


async def test_refresh_no_marketplaces_is_noop(tmp_path: Path) -> None:
    """Empty registry → the inner loop runs zero times — but the
    task is still scheduled and completes cleanly. Important so a
    fresh install (no marketplaces yet) doesn't crash on session
    start."""
    fake_registry = MagicMock()
    fake_registry.marketplaces = []

    with (
        patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load,
        patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh,
    ):
        mock_load.return_value = fake_registry
        sess = _make_session_stub(tmp_path)
        sess.start_marketplace_refresh_background()
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    mock_refresh.assert_not_called()


def test_refresh_without_running_loop_is_noop(tmp_path: Path) -> None:
    """Calling ``start_marketplace_refresh_background`` outside an
    event loop must NOT raise — it bails silently. Mirrors the
    behavior of ``start_codeindex_background``: callers can invoke
    these eagerly from sync contexts (e.g. test harnesses) without
    needing a loop. (This test runs sync so the function sees no
    running loop.)"""
    sess = _make_session_stub(tmp_path)
    # No exception means pass — the early-return on RuntimeError fires.
    sess.start_marketplace_refresh_background()


async def test_refresh_passes_data_dir_through(tmp_path: Path) -> None:
    """The data_dir from ``settings.storage.data_dir`` flows into
    both ``load_registry`` and each ``refresh_marketplace`` call —
    if it weren't forwarded, the refresh would silently read from
    ``~/.ember`` instead of whatever the session's configured
    data dir is (breaking custom XDG-style layouts and tests).

    The refresh task also auto-registers the canonical default
    marketplaces (best-effort), which calls ``load_registry`` a
    second time after the auto-add step. The assertion below
    checks that *every* ``load_registry`` call used the right
    data_dir, not just the first."""
    fake_registry = MagicMock()
    e = MagicMock()
    e.name = "m1"
    fake_registry.marketplaces = [e]
    expected_data_dir = str(tmp_path / "ember")

    with (
        patch("ember_code.core.plugins.marketplaces.load_registry") as mock_load,
        patch("ember_code.core.plugins.marketplaces.refresh_marketplace") as mock_refresh,
        # Auto-register step would otherwise hit the real ``add_marketplace``
        # and try to git-clone the official catalog. Stub it out so the
        # test stays hermetic.
        patch("ember_code.core.plugins.marketplaces.add_marketplace") as mock_add,
    ):
        mock_load.return_value = fake_registry
        mock_refresh.return_value = MagicMock()
        mock_add.return_value = MagicMock()
        sess = _make_session_stub(tmp_path)
        sess.start_marketplace_refresh_background()
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # Every load_registry call carried the configured data_dir.
    assert mock_load.call_count >= 1
    for call in mock_load.call_args_list:
        assert call.args == (expected_data_dir,)
    # refresh_marketplace got the right data_dir as a kwarg.
    refresh_call = mock_refresh.call_args
    assert refresh_call.kwargs["data_dir"] == expected_data_dir
