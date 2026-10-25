"""`@ext.intercept`: the extension answering igni before an event happens.

Against `FakeHost`, which asks as igni asks (`docs/HOST_MODEL_PROTOCOL.md`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from igni_extension import Allow, IgniExtension, Intercepts, Modify, Reject
from igni_extension.host_model import ModelWrite, PlanSubmitting, ToolCalling
from igni_extension.testing import FakeHost

PLAN = {"session_id": "s-1", "plan": "Refactor it.", "tasks": [], "attempt": 0}


@pytest.fixture
async def host() -> AsyncIterator[FakeHost]:
    async with FakeHost(granted=("chat.read",)) as fake:
        yield fake


async def _connected(ext: IgniExtension) -> asyncio.Task[None]:
    await ext.connect()
    return asyncio.create_task(ext.connection.serve_forever())


async def test_a_reject_carries_its_reason(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.intercept(Intercepts.PLAN_SUBMITTING)
    async def grounded(plan: PlanSubmitting) -> Allow | Reject:
        assert plan.attempt == 0
        return Reject("Name the files the plan changes.")

    task = await _connected(ext)
    try:
        answer = await host.intercept("plan.submitting", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "reject"
    assert answer["reason"] == "Name the files the plan changes."


async def test_returning_nothing_allows(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.intercept(Intercepts.UI_NAVIGATE)
    async def watch(write: ModelWrite) -> None:
        assert write.writer == "person"

    task = await _connected(ext)
    try:
        answer = await host.intercept("ui.navigate", {"path": "ui.navigate", "writer": "person"})
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "allow"


async def test_a_change_is_sent_whole(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.intercept(Intercepts.TOOLS_CALLING)
    async def narrow(call: ToolCalling) -> Modify:
        return Modify(call.model_copy(update={"args": {**call.args, "limit": 10}}))

    task = await _connected(ext)
    try:
        answer = await host.intercept(
            "tools.calling", {"session_id": "s-1", "tool": "search", "args": {"q": "x"}}
        )
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "modify"
    assert answer["value"]["args"] == {"q": "x", "limit": 10}
    assert answer["value"]["tool"] == "search"


async def test_a_handler_that_raises_says_it_failed(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.intercept(Intercepts.PLAN_SUBMITTING)
    async def broken(plan: PlanSubmitting) -> Allow:
        raise RuntimeError("index unreachable")

    task = await _connected(ext)
    try:
        answer = await host.intercept("plan.submitting", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer == {
        "intercept": answer["intercept"],
        "outcome": "failed",
        "reason": "index unreachable",
    }


async def test_an_event_nothing_handles_is_allowed(host: FakeHost) -> None:
    ext = IgniExtension("acme.example", environ=host.environment())
    task = await _connected(ext)
    try:
        answer = await host.intercept("plan.submitting", PLAN)
    finally:
        task.cancel()
        await ext.aclose()

    assert answer["outcome"] == "allow"


def test_one_handler_answers_for_an_event() -> None:
    ext = IgniExtension("acme.example", environ={})

    @ext.intercept(Intercepts.PLAN_SUBMITTING)
    async def first(plan: PlanSubmitting) -> None:
        return None

    with pytest.raises(ValueError, match="intercepted twice"):

        @ext.intercept(Intercepts.PLAN_SUBMITTING)
        async def second(plan: PlanSubmitting) -> None:
            return None
