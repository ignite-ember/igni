"""What extensions do to the prompts changed: rebuild every agent, once.

One extension becoming ready announces several times as its edits land;
a rebuild per announcement would rebuild every session's agents over and
over. Coalesced onto the next turn of the loop, it is one.
"""

from __future__ import annotations

import asyncio

import pytest

from ember_code.backend.agent_refresh import AgentRefresh
from ember_code.core.prompts.contributions import EditOp, PromptContributions, PromptEdit

pytestmark = pytest.mark.asyncio


async def test_many_changes_are_one_rebuild():
    rebuilt: list[int] = []
    refresh = AgentRefresh(loop=asyncio.get_running_loop(), refresh=lambda: rebuilt.append(1))
    contributions = PromptContributions()
    contributions.watch(refresh.request)

    contributions.set_owner(
        "a.ext", "Ay", edits=[PromptEdit(target="main", op=EditOp.APPEND, name="s", text="x")]
    )
    contributions.hold("a.ext", {"ready": True})
    contributions.remove_owner("a.ext")
    assert rebuilt == []

    await asyncio.sleep(0)
    assert rebuilt == [1]


async def test_a_later_change_is_another_rebuild():
    rebuilt: list[int] = []
    refresh = AgentRefresh(loop=asyncio.get_running_loop(), refresh=lambda: rebuilt.append(1))
    refresh.request()
    await asyncio.sleep(0)
    refresh.request()
    await asyncio.sleep(0)
    assert rebuilt == [1, 1]


async def test_a_failed_rebuild_keeps_the_agents_and_the_next_one_still_runs(caplog):
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    refresh = AgentRefresh(loop=asyncio.get_running_loop(), refresh=flaky)
    refresh.request()
    await asyncio.sleep(0)
    refresh.request()
    await asyncio.sleep(0)
    assert calls == [1, 1]
    assert "rebuilding the agents" in caplog.text
