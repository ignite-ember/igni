"""Every task or thread igni starts goes through ``BackgroundTasks``.

``docs/CLOUD.md`` §9. Whether igni's process may be suspended is decided
by what holds a busy lease (``core/activity``). A task started any other
way is invisible to that: the process could be suspended under it. So
code under ``src/`` starts work only through
:class:`~ember_code.core.activity.background.BackgroundTasks` — ``start``
for work that finishes (busy until it does), ``daemon`` for a loop that
lives as long as its owner, ``thread`` for a thread.

:data:`ALLOWED` is the reviewed exception list. Adding to it is a
decision that the thing must never count as busy *and* cannot go
through ``daemon``; say why next to it.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from ember_code.core.activity import ActivityRegistry, BackgroundTasks, BusyKind

SRC = Path(__file__).resolve().parents[1] / "src" / "ember_code"

#: The calls that start work behind the caller's back.
STARTERS = {
    "create_task",
    "ensure_future",
    "TaskGroup",
    "Thread",
    "Timer",
    "run_in_executor",
    "start_new_thread",
}

#: ``(path under src/ember_code, call)`` → why it may.
ALLOWED: dict[tuple[str, str], str] = {
    ("core/activity/background.py", "create_task"): "the helper itself",
    ("core/activity/background.py", "ensure_future"): "the helper itself",
    ("core/activity/background.py", "Thread"): "the helper itself",
}


class Starts(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast's name
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in STARTERS:
            self.found.append((node.lineno, name))
        self.generic_visit(node)


def test_no_task_or_thread_is_started_outside_the_helper():
    stray = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        visitor = Starts()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for line, name in visitor.found:
            if (relative, name) not in ALLOWED:
                stray.append(f"{relative}:{line} {name}(...)")
    assert stray == [], (
        "start background work through ember_code.core.activity.background "
        "(start / daemon / thread), or add a reviewed entry to ALLOWED:\n" + "\n".join(stray)
    )


def test_every_allowance_is_still_used():
    used = set()
    for relative, name in ALLOWED:
        visitor = Starts()
        visitor.visit(ast.parse((SRC / relative).read_text(encoding="utf-8")))
        if any(found == name for _, found in visitor.found):
            used.add((relative, name))
    assert used == set(ALLOWED), "remove allowances nothing needs"


#: ``httpx`` clients that may be built without a busy lease, and why.
UNCOUNTED_CLIENTS: dict[str, str] = {
    "notifications/stream.py": "held open for as long as somebody is signed in: not work",
    "core/activity/http.py": "the busy clients themselves",
}


class Clients(ast.NodeVisitor):
    """``httpx.Client(...)`` / ``httpx.AsyncClient(...)`` not under a ``busy_call``."""

    def __init__(self) -> None:
        self.found: list[int] = []
        self._covered = 0

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        covered = any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == "busy_call"
            for item in node.items
        )
        self._covered += covered
        self.generic_visit(node)
        self._covered -= covered

    visit_With = _with  # noqa: N815 - ast's name
    visit_AsyncWith = _with  # noqa: N815 - ast's name

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast's name
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"Client", "AsyncClient"}
            and getattr(func.value, "id", None) == "httpx"
            and not self._covered
        ):
            self.found.append(node.lineno)
        self.generic_visit(node)


def test_every_http_client_holds_a_busy_lease():
    stray = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in UNCOUNTED_CLIENTS:
            continue
        visitor = Clients()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        stray += [f"{relative}:{line}" for line in visitor.found]
    assert stray == [], (
        "build BusyAsyncClient / BusyClient, or wrap the client in busy_call(...) "
        "(core/activity/http.py), or add a reviewed entry to UNCOUNTED_CLIENTS:\n"
        + "\n".join(stray)
    )


@pytest.fixture
def tasks() -> BackgroundTasks:
    return BackgroundTasks(ActivityRegistry())


async def test_started_work_is_busy_until_it_ends(tasks: BackgroundTasks):
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "done"

    future = tasks.start(work(), holder="test", reason="waiting", kind=BusyKind.POST_RUN)
    [lease] = tasks.registry.busy()
    assert (lease.kind, lease.holder, lease.reason) == (BusyKind.POST_RUN, "test", "waiting")
    gate.set()
    assert await future == "done"
    assert tasks.registry.busy() == []


async def test_a_failing_task_lets_go_too(tasks: BackgroundTasks):
    async def boom() -> None:
        raise RuntimeError("boom")

    future = tasks.start(boom(), holder="test")
    with pytest.raises(RuntimeError):
        await future
    assert tasks.registry.busy() == []


async def test_a_daemon_is_never_busy(tasks: BackgroundTasks):
    future = tasks.daemon(asyncio.sleep(3600), name="loop")
    assert tasks.registry.busy() == []
    future.cancel()


def test_a_thread_counts_only_when_asked(tasks: BackgroundTasks):
    release = threading.Event()
    counted = tasks.thread(release.wait, name="worker", counts=True)
    quiet = tasks.thread(release.wait, name="waiter")
    assert [lease.holder for lease in tasks.registry.busy()] == ["worker"]
    release.set()
    counted.join(2)
    quiet.join(2)
    assert tasks.registry.busy() == []


def test_without_a_loop_work_is_dropped_closed(tasks: BackgroundTasks):
    async def work() -> None:
        pass

    coroutine = work()
    assert tasks.start_if_running(coroutine, holder="x") is None
    assert coroutine.cr_frame is None, "closed, so nothing warns it was never awaited"
    assert tasks.registry.busy() == []
