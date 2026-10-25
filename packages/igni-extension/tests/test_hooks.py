"""`@ext.hook`: the extension answering igni's hook events.

Against `FakeHost`, which asks as igni asks (`docs/HOST_MODEL_PROTOCOL.md`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from igni_extension import Allow, Hooks, IgniExtension, Modify, Reject
from igni_extension.hooks import PlanSubmit, PostToolUse, PreToolUse, Told
from igni_extension.testing import FakeHost

PLAN = {"session_id": "s-1", "plan": "Refactor it.", "tasks": [], "attempt": 0}


@pytest.fixture
async def host() -> AsyncIterator[FakeHost]:
    async with FakeHost() as fake:
        yield fake


async def _connected(ext: IgniExtension) -> asyncio.Task[None]:
    await ext.connect()
    return asyncio.create_task(ext.connection.serve_forever())


async def test_a_reject_carries_its_reason(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.hook(Hooks.PLAN_SUBMIT)
    async def grounded(plan: PlanSubmit) -> Allow | Reject:
        assert plan.attempt == 0
        return Reject("Name the files the plan changes.")

    task = await _connected(ext)
    try:
        answer = await host.hook("PlanSubmit", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "reject"
    assert answer["reason"] == "Name the files the plan changes."


async def test_returning_nothing_allows(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.hook(Hooks.POST_TOOL_USE)
    async def watch(call: PostToolUse) -> None:
        assert call.result == "42"

    task = await _connected(ext)
    try:
        answer = await host.hook(
            "PostToolUse",
            {"session_id": "s-1", "tool_name": "sum", "tool_args": {}, "result": "42"},
        )
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "allow"


async def test_a_change_is_sent_whole(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.hook(Hooks.PRE_TOOL_USE)
    async def narrow(call: PreToolUse) -> Modify:
        return Modify(call.model_copy(update={"tool_args": {**call.tool_args, "limit": 10}}))

    task = await _connected(ext)
    try:
        answer = await host.hook(
            "PreToolUse", {"session_id": "s-1", "tool_name": "search", "tool_args": {"q": "x"}}
        )
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "modify"
    assert answer["value"]["tool_args"] == {"q": "x", "limit": 10}
    assert answer["value"]["tool_name"] == "search"


async def test_a_handler_that_raises_says_it_failed(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.hook(Hooks.PLAN_SUBMIT)
    async def broken(plan: PlanSubmit) -> Allow:
        raise RuntimeError("index unreachable")

    task = await _connected(ext)
    try:
        answer = await host.hook("PlanSubmit", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer == {"hook": answer["hook"], "outcome": "failed", "reason": "index unreachable"}


async def test_an_event_nothing_handles_is_allowed(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    task = await _connected(ext)
    try:
        answer = await host.hook("PlanSubmit", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "allow"


async def test_a_told_event_reaches_its_handler_and_nothing_goes_back(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    heard: asyncio.Future[Told] = asyncio.get_running_loop().create_future()

    @ext.hook(Hooks.SESSION_START)
    async def started(event: Told) -> None:
        heard.set_result(event)

    task = await _connected(ext)
    try:
        await host.tell("SessionStart", {"session_id": "s-1", "source": "startup"})
        event = await asyncio.wait_for(heard, 5)
    finally:
        task.cancel()
        await ext.aclose()

    assert event.session_id == "s-1"
    assert event.model_extra == {"source": "startup"}


def test_one_handler_answers_for_an_event() -> None:
    ext = IgniExtension("acme.example", environ={})

    @ext.hook(Hooks.PLAN_SUBMIT)
    async def first(plan: PlanSubmit) -> None:
        return None

    with pytest.raises(ValueError, match="hooked twice"):

        @ext.hook(Hooks.PLAN_SUBMIT)
        async def second(plan: PlanSubmit) -> None:
            return None


def test_only_the_deciding_events_can_be_changed() -> None:
    changeable = {event.name: event.modify for event in Hooks.all() if event.modify}
    assert changeable == {
        "PreToolUse": ("tool_args",),
        "PostToolUse": ("result",),
        "UserPromptSubmit": ("message",),
        "PlanSubmit": ("plan",),
    }
    assert {event.name for event in Hooks.all() if event.decides} == {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Stop",
        "PlanSubmit",
    }
