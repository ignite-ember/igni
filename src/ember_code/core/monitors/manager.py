"""MonitorManager — per-session lifecycle for plugin-declared
background monitors.

Slim orchestrator: constructs :class:`MonitorHandle` and
:class:`MonitorSupervisor` instances, keeps them in per-name
dicts, and forwards public control calls
(``start_all``/``restart``/``stop``/``shutdown_all``) onto them.
All process I/O and status transitions live on the handle; all
restart-policy logic lives on the supervisor + evaluator; all
DTOs live in :mod:`.models`.

Why a separate manager (vs. reusing ``_ProcessRegistry``):
``_ProcessRegistry`` tracks agent-spawned shell processes whose
lifecycle is tied to a single shell tool call. Monitors are
session-scoped, plugin-owned, auto-restarted, and the agent
observes them through query tools rather than directly spawning
them. Different threat model → different manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from ember_code.core.monitors.config import MonitorConfig
from ember_code.core.monitors.handle import MonitorHandle
from ember_code.core.monitors.models import (
    MonitorControlResult,
    MonitorSnapshot,
    MonitorStatus,
)
from ember_code.core.monitors.supervisor import (
    MonitorSupervisor,
    RestartPolicyEvaluator,
)

# Backwards-compat re-exports so
# ``from ember_code.core.monitors.manager import MonitorHandle``
# (existing test imports, potential external callers) keeps
# working after the split. New code should import these from the
# package root or their owning modules.
__all__ = [
    "MonitorControlResult",
    "MonitorHandle",
    "MonitorManager",
    "MonitorSnapshot",
    "MonitorStatus",
]

logger = logging.getLogger(__name__)


class MonitorManager:
    """Per-session manager — holds every plugin monitor."""

    def __init__(
        self,
        monitors: dict[str, MonitorConfig],
        project_dir: Path,
    ) -> None:
        self._configs = dict(monitors)
        self._project_dir = project_dir
        self._handles: dict[str, MonitorHandle] = {}
        self._supervisors: dict[str, asyncio.Task] = {}
        self._evaluator = RestartPolicyEvaluator()

    def list_names(self) -> list[str]:
        return sorted(self._configs.keys())

    def is_configured(self, name: str) -> bool:
        """Public replacement for the ``name in manager._configs``
        reach-in the agent Toolkit was doing."""
        return name in self._configs

    def snapshot_all(self) -> list[MonitorSnapshot]:
        """Status snapshot for every configured monitor (even ones
        we haven't started yet — they show ``status=stopped``)."""
        out: list[MonitorSnapshot] = []
        for name in self.list_names():
            handle = self._handles.get(name)
            if handle is None:
                cfg = self._configs[name]
                out.append(
                    MonitorSnapshot(
                        name=name,
                        command=cfg.command,
                        status=MonitorStatus.STOPPED,
                        pid=None,
                        uptime_seconds=0.0,
                        exit_code=None,
                        crash_count=0,
                        restart=cfg.restart,
                    )
                )
            else:
                out.append(handle.snapshot())
        return out

    def output_tail(self, name: str, lines: int = 40) -> list[str]:
        handle = self._handles.get(name)
        if handle is None:
            return []
        return handle.output_tail(lines)

    async def start_all(self) -> None:
        """Launch every configured monitor + its supervisor.
        Idempotent — running monitors aren't restarted."""
        for name in self._configs:
            await self._start_one(name)

    async def _start_one(self, name: str) -> MonitorHandle:
        config = self._configs[name]
        handle = self._handles.get(name)
        if handle is None:
            handle = MonitorHandle(config, project_dir=self._project_dir)
            self._handles[name] = handle
        if handle.status is not MonitorStatus.RUNNING:
            await handle.start()
        if name not in self._supervisors or self._supervisors[name].done():
            supervisor = MonitorSupervisor(handle, self._evaluator)
            self._supervisors[name] = asyncio.create_task(
                supervisor.run(), name=f"monitor-supervisor-{name}"
            )
        return handle

    async def restart(self, name: str) -> MonitorControlResult:
        """User-initiated restart — clears the crash counter and
        re-launches even a ``failed`` monitor."""
        if name not in self._configs:
            return MonitorControlResult(
                ok=False,
                name=name,
                action="restart",
                reason=f"Monitor not configured: {name!r}",
            )
        await self.stop(name)
        handle = self._handles.get(name)
        if handle is not None:
            handle.reset_crash_count()
        await self._start_one(name)
        return MonitorControlResult(
            ok=True,
            name=name,
            action="restart",
            reason=f"Restarted {name}.",
        )

    async def stop(self, name: str) -> MonitorControlResult:
        if name not in self._configs:
            return MonitorControlResult(
                ok=False,
                name=name,
                action="stop",
                reason=f"Monitor not configured: {name!r}",
            )
        sup = self._supervisors.pop(name, None)
        if sup is not None and not sup.done():
            sup.cancel()
            await self._await_task_swallowing_cancel(sup)
        handle = self._handles.get(name)
        if handle is not None:
            await handle.stop()
        return MonitorControlResult(
            ok=True,
            name=name,
            action="stop",
            reason=f"Stopped {name}.",
        )

    async def shutdown_all(self) -> None:
        """Tear down every monitor + supervisor. Safe to call
        multiple times — already-stopped monitors are no-ops."""
        names = list(self._handles.keys())
        for name in names:
            await self.stop(name)
        self._handles.clear()
        self._supervisors.clear()

    @staticmethod
    async def _await_task_swallowing_cancel(task: asyncio.Task) -> None:
        """Await a cancelled task and swallow both CancelledError
        (BaseException in 3.8+) and any downstream Exception the
        supervisor might have raised on its way out.

        Named for intent so the reader doesn't have to decode a
        bare ``contextlib.suppress`` — see the audit note on
        AP6 (comments-explain-what-name-should-explain)."""
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
