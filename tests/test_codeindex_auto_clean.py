"""Tests for the auto-clean hook in ``Session.start_codeindex_background``.

After the initial ``sync_now``, every session prunes its own stale
commit chromas via ``CodeIndex.clean()`` (drops anything that isn't
HEAD, isn't a branch tip, and hasn't been touched in 30 days). This
keeps long-lived users' ``~/.ember/projects/<id>/code_index/`` from
growing without bound as branches/checkouts churn.

We construct a Session-like stub by hand (the real
``Session.__init__`` would pull in DB / MCP / pools) and verify the
call order: sync_now → clean → start_watcher. The actual eviction
logic in ``CodeIndex.clean`` is exercised by ``test_code_index.py``;
here we only pin the wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ember_code.core.session.core import Session


def _make_session_stub(tmp_path: Path) -> Session:
    """A minimal Session object carrying just what
    ``start_codeindex_background`` reads."""
    sess = Session.__new__(Session)
    sess.settings = MagicMock()
    sess.settings.storage.data_dir = str(tmp_path / "ember")
    sess.code_index = MagicMock()
    sess.code_index.clean = AsyncMock(return_value=[])
    sess.code_index.sweep_stale_dirs = MagicMock(return_value=[])
    sess.code_index_sync = MagicMock()
    sess.code_index_sync.sync_now = AsyncMock()
    sess.code_index_sync.start_watcher = AsyncMock()
    return sess


async def _await_background_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_bootstrap_runs_sweep_then_sync_then_clean_then_watcher(tmp_path: Path) -> None:
    """Order matters and each step needs the previous one's state:

    1. ``sweep_stale_dirs`` rmtrees orphaned chroma directories
       from prior sessions — must run BEFORE any client is opened
       in this process (chromadb's process-level cache would
       otherwise hold stale handles to paths we just deleted).
    2. ``sync_now`` refreshes HEAD's ``last_used_at`` so ``clean``
       can't accidentally evict the current commit.
    3. ``clean`` drops idle commits via ``delete_collection`` —
       safe regardless of cached clients.
    4. ``start_watcher`` enables steady-state head-following.
    """
    order: list[str] = []
    sess = _make_session_stub(tmp_path)
    sess.code_index.sweep_stale_dirs = MagicMock(side_effect=lambda: order.append("sweep") or [])
    sess.code_index_sync.sync_now = AsyncMock(side_effect=lambda *a, **kw: order.append("sync"))
    sess.code_index.clean = AsyncMock(side_effect=lambda *a, **kw: order.append("clean") or [])
    sess.code_index_sync.start_watcher = AsyncMock(
        side_effect=lambda *a, **kw: order.append("watcher")
    )

    sess.start_codeindex_background()
    await _await_background_tasks()

    assert order == ["sweep", "sync", "clean", "watcher"]


async def test_bootstrap_swallows_sweep_failure(tmp_path: Path) -> None:
    """``sweep_stale_dirs`` failing (permission issue, race) must not
    block the rest of the bootstrap — sync + clean + watcher still
    run."""
    sess = _make_session_stub(tmp_path)
    sess.code_index.sweep_stale_dirs = MagicMock(side_effect=OSError("permission denied"))

    sess.start_codeindex_background()
    await _await_background_tasks()

    sess.code_index.sweep_stale_dirs.assert_called_once()
    sess.code_index_sync.sync_now.assert_awaited_once()
    sess.code_index.clean.assert_awaited_once()
    sess.code_index_sync.start_watcher.assert_awaited_once()


async def test_bootstrap_passes_default_30_day_cutoff(tmp_path: Path) -> None:
    """``clean()`` is invoked without overriding ``keep_recent_days``,
    so the default 30-day retention applies. If anyone ever changes
    the default and silently shortens retention, this test catches it."""
    sess = _make_session_stub(tmp_path)
    sess.start_codeindex_background()
    await _await_background_tasks()

    sess.code_index.clean.assert_awaited_once()
    # No positional args and no keyword override means the 30-day
    # default in ``CodeIndex.clean`` is in effect.
    call = sess.code_index.clean.call_args
    assert call.args == ()
    assert call.kwargs == {}


async def test_bootstrap_swallows_clean_failure(tmp_path: Path) -> None:
    """If ``clean`` raises (corrupt manifest, unreachable git, etc.),
    the bootstrap must continue to ``start_watcher`` — auto-clean is
    a housekeeping nice-to-have, not a prerequisite for syncing."""
    sess = _make_session_stub(tmp_path)
    sess.code_index.clean = AsyncMock(side_effect=RuntimeError("manifest corrupt"))

    sess.start_codeindex_background()
    await _await_background_tasks()

    sess.code_index_sync.sync_now.assert_awaited_once()
    sess.code_index.clean.assert_awaited_once()
    sess.code_index_sync.start_watcher.assert_awaited_once()


def test_bootstrap_without_running_loop_is_noop(tmp_path: Path) -> None:
    """Sync caller (no running event loop) bails silently. Matches
    the existing contract of ``start_codeindex_background`` — callers
    can invoke from sync contexts without a loop."""
    sess = _make_session_stub(tmp_path)
    # No exception means pass — the early-return on RuntimeError fires.
    sess.start_codeindex_background()
    sess.code_index_sync.sync_now.assert_not_awaited()
    sess.code_index.clean.assert_not_awaited()
