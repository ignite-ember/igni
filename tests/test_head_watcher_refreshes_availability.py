"""A background sync that populates the index tells the session about it.

``HeadWatcher`` polls git HEAD once a second and runs a sync when it moves. It
stopped there. Nothing called ``refresh_codeindex_availability``, so a branch
flip that populated the index mid-session left the agent on the toolset and
prompt it was built with — still being told CodeIndex was unavailable while the
graph sat there, queryable.

It is the expensive direction of the two. Availability is decided once at
session construction, and the session that most needs re-deciding is exactly the
long-lived one where someone switches branches.

Wired as an injected callback rather than an import: the watcher is deliberately
free of session knowledge, taking closures for everything it touches.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ember_code.core.code_index.sync.head_watcher import HeadWatcher
from ember_code.core.code_index.sync.retry_ledger import InProgressRetryLedger


@dataclass
class _Result:
    in_progress: bool = False


def _watcher(*, run_sync, on_sync_complete, head="abc123"):
    return HeadWatcher(
        get_head=lambda: head,
        run_sync=run_sync,
        retry_ledger=InProgressRetryLedger(),
        last_synced_sha_getter=lambda: None,
        interval_seconds=0.01,
        on_sync_complete=on_sync_complete,
    )


async def _run_briefly(watcher):
    await watcher.start()
    await asyncio.sleep(0.08)
    await watcher.stop()


class TestTheRefreshFires:
    async def test_a_completed_sync_refreshes_availability(self):
        calls: list[int] = []

        await _run_briefly(
            _watcher(
                run_sync=lambda sha: _completed(),
                on_sync_complete=lambda: calls.append(1),
            )
        )

        assert calls, "a finished sync did not refresh availability"

    async def test_an_in_progress_sync_does_not(self):
        """Still running server-side means the index is not ready to query.

        Refreshing here would flip the session to "available" on the strength of
        a sync that has not landed, which is the opposite failure.
        """
        calls: list[int] = []

        await _run_briefly(
            _watcher(
                run_sync=lambda sha: _in_progress(),
                on_sync_complete=lambda: calls.append(1),
            )
        )

        assert not calls

    async def test_an_async_refresh_is_awaited(self):
        """The session's refresher may be a coroutine; a bare call would
        create a coroutine object and drop it, refreshing nothing."""
        calls: list[int] = []

        async def refresh():
            calls.append(1)

        await _run_briefly(_watcher(run_sync=lambda sha: _completed(), on_sync_complete=refresh))

        assert calls

    async def test_a_raising_refresh_does_not_kill_the_poll_loop(self):
        """The watcher is the only thing keeping the index current. A refresh
        that throws must not silently end polling for the rest of the session."""
        syncs: list[int] = []

        def boom():
            raise RuntimeError("refresh exploded")

        watcher = _watcher(
            run_sync=lambda sha: _completed(syncs),
            on_sync_complete=boom,
        )
        await watcher.start()
        await asyncio.sleep(0.1)
        await watcher.stop()

        assert len(syncs) >= 2, f"polling stopped after the first failure ({len(syncs)} syncs)"


async def _completed(record: list | None = None):
    if record is not None:
        record.append(1)
    return _Result(in_progress=False)


async def _in_progress():
    return _Result(in_progress=True)
