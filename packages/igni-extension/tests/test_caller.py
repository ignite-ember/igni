"""`Caller`: every call says whom it is for, and the extension keeps callers apart.

igni's `docs/EXTENSION_ROUTING_AND_CONCURRENCY.md` §6. A tool or a hook
handler that declares a parameter typed `Caller` is handed it; the
caller is never part of a tool's input; concurrent calls each see their
own; `ext.service.for_(caller)` fetches that caller's token.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from igni_extension import Caller, CallerKind, Hooks, HostError, IgniExtension, Reject, ToolHints
from igni_extension.caller import _SCOPE, NoCaller
from igni_extension.hooks import PlanSubmit
from igni_extension.testing import (
    ConcurrencyHarness,
    ConcurrencyProblem,
    ConcurrentCall,
    FakeHost,
    FakeHostTokenEndpoint,
    HostHarness,
)

HINTS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)
ADA = Caller(kind=CallerKind.PERSON, id="u-ada", org="org-acme", groups=("g-legal",))
GRACE = Caller(kind=CallerKind.PERSON, id="u-grace", org="org-acme", groups=("g-finance",))
PLAN = {"session_id": "s-1", "plan": "Refactor it.", "tasks": [], "attempt": 0}


def _text(result: object) -> str:
    return "".join(block.text for block in result.content)  # type: ignore[attr-defined]


class TestATool:
    async def test_is_handed_its_caller(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def whoami(prefix: str, caller: Caller) -> str:
            return f"{prefix}{caller.id}:{','.join(caller.groups)}"

        async with HostHarness(ext) as host:
            result = await host.call("whoami", {"prefix": "it is "}, caller=ADA)

        assert _text(result) == "it is u-ada:g-legal"

    async def test_the_caller_is_not_part_of_its_input(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def whoami(prefix: str, caller: Caller) -> str:
            return prefix

        [tool] = await ext.mcp.list_tools()

        assert set(tool.inputSchema["properties"]) == {"prefix"}
        assert tool.inputSchema.get("required") == ["prefix"]

    async def test_a_plain_def_in_a_worker_thread_sees_its_caller(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        def whoami(caller: Caller) -> str:
            return caller.id

        async with HostHarness(ext) as host:
            result = await host.call("whoami", caller=GRACE)

        assert _text(result) == "u-grace"

    async def test_anything_inside_the_call_can_ask_for_it(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def whoami() -> str:
            caller = Caller.current()
            return caller.id if caller else "nobody"

        async with HostHarness(ext) as host:
            with_caller = await host.call("whoami", caller=ADA)
            without = await host.call("whoami")

        assert _text(with_caller) == "u-ada"
        assert _text(without) == "nobody"

    async def test_a_tool_that_needs_a_caller_fails_without_one(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def whoami(caller: Caller) -> str:
            return caller.id

        async with HostHarness(ext) as host:
            result = await host.call("whoami")

        assert result.isError
        assert "no caller" in _text(result)

    async def test_an_optional_caller_is_none_without_one(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def whoami(caller: Caller | None) -> str:
            return caller.id if caller else "nobody"

        async with HostHarness(ext) as host:
            result = await host.call("whoami")

        assert _text(result) == "nobody"

    def test_outside_a_call_there_is_none(self) -> None:
        assert Caller.current() is None
        with pytest.raises(NoCaller):
            Caller.required()


@pytest.fixture
async def fake_host() -> AsyncIterator[FakeHost]:
    async with FakeHost() as fake:
        yield fake


class TestAHook:
    async def test_a_handler_that_asks_is_handed_its_caller(self, fake_host: FakeHost) -> None:
        ext = IgniExtension("acme.example", environ=fake_host.environment())

        @ext.hook(Hooks.PLAN_SUBMIT)
        async def grounded(plan: PlanSubmit, caller: Caller) -> Reject:
            return Reject(f"{caller.id} in {caller.org}")

        await ext.connect()
        task = asyncio.create_task(ext.connection.serve_forever())
        try:
            answer = await fake_host.hook("PlanSubmit", PLAN, caller=ADA)
        finally:
            task.cancel()
            await ext.aclose()

        assert answer["reason"] == "u-ada in org-acme"

    async def test_any_handler_can_ask_for_it(self, fake_host: FakeHost) -> None:
        ext = IgniExtension("acme.example", environ=fake_host.environment())

        @ext.hook(Hooks.PLAN_SUBMIT)
        def grounded(plan: PlanSubmit) -> Reject:
            caller = Caller.current()
            return Reject(caller.id if caller else "nobody")

        await ext.connect()
        task = asyncio.create_task(ext.connection.serve_forever())
        try:
            answer = await fake_host.hook("PlanSubmit", PLAN, caller=GRACE)
        finally:
            task.cancel()
            await ext.aclose()

        assert answer["reason"] == "u-grace"


class TestItsService:
    def _ext(self, host: FakeHostTokenEndpoint, seen: list[httpx.Request]) -> IgniExtension:
        def service(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        return IgniExtension(
            "acme.example",
            environ=host.environment(),
            token_transport=host.transport,
            service_transport=httpx.MockTransport(service),
        )

    async def test_for_a_caller_names_them_to_the_host(self) -> None:
        host = FakeHostTokenEndpoint(user_id="u-ada")
        seen: list[httpx.Request] = []
        ext = self._ext(host, seen)

        await ext.service.for_(ADA).get("/v1/things")

        assert host.requests[-1].headers[Caller.TOKEN_HEADER] == "person:u-ada"
        assert seen[-1].headers["authorization"] == f"Bearer {host.issued[-1]}"
        await ext.aclose()

    async def test_the_host_will_not_hand_out_somebody_elses_token(self) -> None:
        host = FakeHostTokenEndpoint(user_id="u-ada")
        ext = self._ext(host, [])

        with pytest.raises(HostError) as refused:
            await ext.service.for_(GRACE).get("/v1/things")

        assert refused.value.code == "refused"
        await ext.aclose()

    async def test_each_caller_has_its_own_client(self) -> None:
        host = FakeHostTokenEndpoint(user_id="u-ada")
        ext = self._ext(host, [])

        assert ext.service.for_(ADA) is ext.service.for_(ADA)
        assert ext.service.for_(ADA) is not ext.service.for_(GRACE)
        await ext.aclose()


class TestTheConcurrencyHarness:
    async def test_callers_served_at_once_stay_apart(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def mine(caller: Caller) -> str:
            await asyncio.sleep(0.05)
            return caller.id

        @ext.tool(hints=HINTS)
        def mine_slowly(caller: Caller) -> str:
            time.sleep(0.2)
            return caller.id

        report = await ConcurrencyHarness(ext).run(
            [
                ConcurrentCall(tool="mine", caller=ADA),
                ConcurrentCall(tool="mine_slowly", caller=GRACE),
                ConcurrentCall(tool="mine", caller=GRACE),
                ConcurrentCall(tool="mine_slowly", caller=ADA),
            ]
        )

        answered = [str(result.result) for result in report.results]
        assert "u-ada" in answered[0] and "u-grace" in answered[1]
        assert "u-grace" in answered[2] and "u-ada" in answered[3]
        assert report.leaks == []

    async def test_blocking_the_loop_fails(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def blocks(caller: Caller) -> str:
            time.sleep(0.3)  # noqa: ASYNC251 — the mistake the harness exists to catch
            return caller.id

        with pytest.raises(ConcurrencyProblem, match="event loop was held"):
            await ConcurrencyHarness(ext).run(
                [
                    ConcurrentCall(tool="blocks", caller=ADA),
                    ConcurrentCall(tool="blocks", caller=GRACE),
                ]
            )

    async def test_a_call_that_sees_another_caller_fails(self) -> None:
        ext = IgniExtension("acme.example", environ={})

        @ext.tool(hints=HINTS)
        async def leaks() -> str:
            # The caller set and never put back: what a caller kept in a
            # global looks like from inside the call.
            _SCOPE.set(GRACE)
            return "done"

        with pytest.raises(ConcurrencyProblem, match="saw caller 'person:u-grace'"):
            await ConcurrencyHarness(ext).run([ConcurrentCall(tool="leaks", caller=ADA)])
