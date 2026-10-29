"""What the warm base holds stays the base's pages, and igni gets ready for a suspend.

``docs/CLOUD_RESUME_TODO.md`` item 2. A page a producer's igni writes to
becomes its own: stored at suspend, read back on a cold resume. So the
objects alive when the warm base is taken are made immortal — taking a
reference writes no count — with their strings' hashes and their
classes' version tags filled in (:mod:`igni_cloud.guest.immortal`); and
before a suspend igni hands its free heap back
(:mod:`igni_cloud.guest.suspend`).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest.immortal import BaseObjects
from igni_cloud.guest.suspend import HeapTrim, MallInfo2, SuspendWatch

HOOK = Path(__file__).parents[1] / "hooks" / "before-suspend.d" / "50-igni"

# Run in a process of its own: it makes everything alive there immortal.
PROBE = """
import ctypes, json, sys
from igni_cloud.guest.immortal import BaseObjects

class Base:
    pass

name = "".join(["warm", "-", "base", "-", "string"])  # not interned
before = ctypes.c_ssize_t.from_address(id(name)).value
done = BaseObjects.immortalise()
immortal = BaseObjects.immortal_refcount()
held = [name] * 100
after = ctypes.c_ssize_t.from_address(id(name)).value
new = "".join(["made", "-", "later"])
print(json.dumps({
    "done": done.model_dump() if done else None,
    "before": before, "after": after, "immortal": immortal,
    "hash_cached": ctypes.c_ssize_t.from_address(id(name) + 24).value != -1,
    "tagged": (Base.__flags__ & (1 << 19)) != 0,
    "new_is_mortal": ctypes.c_ssize_t.from_address(id(new)).value != immortal,
}))
"""


@pytest.mark.skipif(BaseObjects.immortal_refcount() is None, reason="CPython 3.12+, 64-bit")
def test_every_object_alive_at_the_warm_base_is_immortal() -> None:
    out = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, check=True
    ).stdout
    result = json.loads(out)
    assert result["done"]["objects"] > 1000
    assert result["after"] == result["immortal"], "a hundred references wrote nothing"
    assert result["before"] != result["immortal"]
    assert result["hash_cached"], "its hash computed in the base, not on first use"
    assert result["tagged"], "the class's version tag assigned in the base"
    assert result["new_is_mortal"], "what igni makes afterwards is freed as usual"


def test_where_objects_are_not_immortal_that_way_it_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BaseObjects, "immortal_refcount", staticmethod(lambda: None))
    assert BaseObjects.immortalise() is None


async def test_a_suspending_request_is_answered_once(tmp_path: Path) -> None:
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready", watch=False)
    (tmp_path / "suspending").write_text(json.dumps({"token": "t1"}))
    answer = await watch.check()
    assert answer is not None and answer.token == "t1"
    assert json.loads((tmp_path / "suspend-ready").read_text())["token"] == "t1"
    assert await watch.check() is None, "once per request"
    if sys.platform.startswith("linux"):
        assert answer.trimmed_kb is not None
        assert answer.free_heap_kb is not None
    else:
        assert HeapTrim.run() is None


async def test_the_hook_waits_for_igni_and_never_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SuspendWatch, "POLL_SECONDS", 0.01)
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready", watch=False)
    watch.start()
    env = {**os.environ, "IGNI_RUN_DIR": str(tmp_path)}
    try:
        hook = await asyncio.create_subprocess_exec(
            str(HOOK), env=env, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(hook.communicate(), 5)
        assert hook.returncode == 0
        assert b"did not answer" not in err
        token = json.loads((tmp_path / "suspending").read_text())["token"]
        assert json.loads((tmp_path / "suspend-ready").read_text())["token"] == token
    finally:
        await watch.stop()
    # Nobody answering: the hook gives up within half a second, and succeeds.
    hook = await asyncio.create_subprocess_exec(str(HOOK), env=env, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(hook.communicate(), 5)
    assert hook.returncode == 0 and b"did not answer" in err


async def test_steps_before_a_suspend_run_first_and_a_slow_one_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SuspendWatch, "STEPS_SECONDS", 0.05)
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready", watch=False)
    ran: list[str] = []

    async def close_connections() -> None:
        ran.append("closed")

    async def slow() -> None:
        await asyncio.sleep(5)

    watch.before_suspend(close_connections)
    watch.before_suspend(slow)
    (tmp_path / "suspending").write_text(json.dumps({"token": "t2"}))
    answer = await watch.check()
    assert ran == ["closed"]
    assert answer is not None and answer.unfinished == ["slow"], "answered within the hook's wait"


class FakeLibc:
    """``malloc_trim`` and ``mallinfo2`` as glibc answers them, with this much free."""

    def __init__(self, free_kb: int) -> None:
        self.trims: list[int] = []
        self.mallinfo2 = self.MallInfo(free_kb)

    def malloc_trim(self, pad: int) -> int:
        self.trims.append(pad)
        return 1

    class MallInfo:
        def __init__(self, free_kb: int) -> None:
            self.free_kb = free_kb
            self.restype: Any = None

        def __call__(self) -> MallInfo2:
            return MallInfo2(fordblks=self.free_kb * 1024)


@pytest.mark.parametrize(("free_kb", "trimmed"), [(12 * 1024, False), (200 * 1024, True)])
def test_only_a_large_free_heap_is_given_back(
    monkeypatch: pytest.MonkeyPatch, free_kb: int, trimmed: bool
) -> None:
    libc = FakeLibc(free_kb)
    monkeypatch.setattr(HeapTrim, "_libc", classmethod(lambda cls: libc))
    free = HeapTrim.free_kb()
    assert free == free_kb
    result = HeapTrim.run(free)
    assert bool(libc.trims) is trimmed, "a usual turn's free heap is the next turn's"
    if not trimmed:
        assert result == 0


async def test_the_answer_says_how_much_was_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    libc = FakeLibc(1234)
    monkeypatch.setattr(HeapTrim, "_libc", classmethod(lambda cls: libc))
    watch = SuspendWatch(tmp_path / "suspending", tmp_path / "suspend-ready", watch=False)
    (tmp_path / "suspending").write_text(json.dumps({"token": "t3"}))
    answer = await watch.check()
    assert answer is not None
    assert (answer.free_heap_kb, answer.trimmed_kb) == (1234, 0)
    assert libc.trims == []
