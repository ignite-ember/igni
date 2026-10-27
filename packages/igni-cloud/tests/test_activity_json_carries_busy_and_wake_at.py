"""``activity.json`` carries what holds igni busy and when to wake it.

The node agent reads it (``docs/CLOUD.md`` §9): ``busy`` as
``{kind, holder, reason, since, expires_at}``, ``wake_at``, times in Unix
milliseconds, and a heartbeat while igni is not suspendable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from igni_cloud.guest import ActivityFile

from ember_code.backend.activity import Activity
from ember_code.core.activity import ActivityRegistry, BusyKind


def test_the_activity_file_carries_busy_and_wake_at(tmp_path: Path):
    registry = ActivityRegistry()
    wake = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    registry.wake("scheduler:a", wake, "a scheduled task")
    registry.hold(BusyKind.MONITOR, "monitor:ci", "watching")
    file = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.now([], registry), clients=lambda: 0
    )
    assert file.refresh()
    written = json.loads(file.path.read_text())
    assert written["suspendable"] is False and written["state"] == "running"
    [lease] = written["busy"]
    assert {"kind", "holder", "reason", "since", "expires_at"} <= set(lease)
    assert (lease["kind"], lease["holder"]) == ("monitor", "monitor:ci")
    assert written["wake_at"] == int(wake.timestamp() * 1000), "Unix milliseconds"
    assert isinstance(lease["since"], int)


async def test_busy_but_not_running_still_gets_its_heartbeat(tmp_path: Path):
    registry = ActivityRegistry()
    now = [1000.0]
    registry.hold(BusyKind.PROCESS, "process:7")
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.now([], registry),
        clients=lambda: 0,
        clock=lambda: now[0],
    )
    assert file.refresh()
    now[0] += ActivityFile.HEARTBEAT_SECONDS
    assert file.refresh(), "rewritten so the guest agent does not call it stale"
    await asyncio.sleep(0)
