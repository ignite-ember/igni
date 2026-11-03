"""Every message a client sends is routed, answered under its id, and mirrored.

:class:`MessageDispatcher` is the one place the receive loop hands a
parsed wire message to. Its RPC path had tests; the chat paths — the
user message, the HITL answer, the batch answer, the slash command, the
session and model switches, the queued message, the typing relay,
cancel — did not, although they are what every turn goes through.

What is pinned:

* each message reaches the backend method that serves it, and the reply
  goes out stamped with the request's id so the client can match it;
* a streamed reply ends in ``StreamEnd`` with the same id;
* the user's bubble and a resolved permission dialog are mirrored to
  every view *before* the run streams;
* ``force`` (the "Retry" on an interrupted run) clears the stale pending
  rows before the new run is recorded;
* a ``/plan`` armed on the session wraps the next prompt once;
* the session is named after the first run, and the name is pushed only
  when one was made;
* an extension's message is labelled as the extension's, never as the
  person's, and the agent is told who sent it;
* an RPC whose handler is synchronous, returns a wire message, or returns
  an exotic value still answers.

The backend is a small recording double: it is the seam, and the real
one needs a booted session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from ember_code.backend.message_dispatcher import ExtensionMessage, MessageDispatcher
from ember_code.backend.schemas_sessions import AutoNameResult
from ember_code.core.activity import background
from ember_code.extensions.registry import ExtensionRefused
from ember_code.protocol import messages as msg


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Journal:
    def __init__(self) -> None:
        self.discards = 0

    async def discard_all_for_session(self) -> int:
        self.discards += 1
        return 2


class _Runs:
    def __init__(self) -> None:
        self.pending_journal = _Journal()


class _Runtime:
    def __init__(self) -> None:
        self.spawned: list[Any] = []

    def spawn_auto_name(self, coro: Any) -> None:
        self.spawned.append(coro)


class _Backend:
    """Records what the dispatcher asked of it; answers like the real one."""

    def __init__(self, *, runtime: _Runtime | None = None, auto_name: str = "") -> None:
        self.runtime = runtime
        self.session_id = "s-1"
        self._runs = _Runs()
        self.plan_armed = False
        self.auto_name = auto_name
        self.calls: list[tuple[str, Any]] = []
        self.cancelled = 0

    def consume_plan_research_flag(self) -> bool:
        armed, self.plan_armed = self.plan_armed, False
        return armed

    async def run_message(self, text: str, media: dict[str, str]) -> AsyncIterator[Any]:
        self.calls.append(("run_message", (text, media)))
        yield msg.Info(text="thinking")
        yield msg.Info(text="done")

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str
    ) -> AsyncIterator[Any]:
        self.calls.append(("resolve_hitl", (requirement_id, action, choice)))
        yield msg.Info(text="resumed")

    async def resolve_hitl_batch(self, decisions: list[Any]) -> AsyncIterator[Any]:
        self.calls.append(("resolve_hitl_batch", [d.requirement_id for d in decisions]))
        yield msg.Info(text="resumed all")

    async def handle_command(self, text: str) -> Any:
        self.calls.append(("handle_command", text))
        return msg.Info(text=f"ran {text}")

    async def list_sessions(self) -> Any:
        self.calls.append(("list_sessions", None))
        return msg.Info(text="sessions")

    async def switch_session(self, session_id: str) -> Any:
        self.calls.append(("switch_session", session_id))
        return msg.Info(text=f"now {session_id}")

    def switch_model(self, model_name: str) -> Any:
        self.calls.append(("switch_model", model_name))
        return msg.Info(text=f"model {model_name}")

    async def toggle_mcp(self, server_name: str, connect: bool) -> Any:
        self.calls.append(("toggle_mcp", (server_name, connect)))
        return msg.Info(text="toggled")

    def cancel_run(self) -> None:
        self.cancelled += 1

    async def maybe_auto_name_session(self) -> AutoNameResult:
        if self.auto_name:
            return AutoNameResult(ok=True, name=self.auto_name)
        return AutoNameResult(ok=False, reason="already_named")


class _Login:
    def __init__(self) -> None:
        self.cancelled = 0

    async def cancel(self) -> None:
        self.cancelled += 1


def _dispatcher(
    backend: _Backend | None = None,
    *,
    table: dict | None = None,
    login: Any = None,
    extensions: Any = None,
) -> tuple[MessageDispatcher, _Transport, list[str], _Backend]:
    backend = backend or _Backend(runtime=_Runtime())
    transport = _Transport()
    queue: list[str] = []
    dispatcher = MessageDispatcher(
        backend=backend,
        transport=transport,
        rpc_table=table or {},
        queue=queue,
        login=login,
        extensions=extensions,
    )
    return dispatcher, transport, queue, backend


def _types(sent: list[Any]) -> list[str]:
    return [m.type for m in sent]


# ── The chat paths ────────────────────────────────────────────────


async def test_a_user_message_is_mirrored_then_streamed_under_its_id():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.dispatch(
        msg.UserMessage(id="r1", text="hello", client_id="tab-a", file_contents={"a.py": "x"})
    )

    assert _types(transport.sent) == ["user_message_received", "info", "info", "stream_end"]
    echo = transport.sent[0]
    assert (echo.text, echo.client_id) == ("hello", "tab-a")
    assert all(m.id == "r1" for m in transport.sent[1:])
    assert backend.calls == [("run_message", ("hello", {"a.py": "x"}))]
    assert len(backend.runtime.spawned) == 1
    backend.runtime.spawned[0].close()


async def test_a_message_without_an_id_streams_unstamped():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.dispatch(msg.UserMessage(text="hi"))

    assert not any(m.id for m in transport.sent[1:])
    backend.runtime.spawned[0].close()


async def test_retry_clears_the_stale_pending_rows_first():
    dispatcher, _, _, backend = _dispatcher()

    await dispatcher.dispatch(msg.UserMessage(id="r", text="again", force=True))

    assert backend._runs.pending_journal.discards == 1
    backend.runtime.spawned[0].close()


async def test_retry_on_a_backend_without_a_run_journal_still_runs():
    backend = _Backend(runtime=_Runtime())
    del backend._runs
    dispatcher, transport, _, _ = _dispatcher(backend)

    await dispatcher.dispatch(msg.UserMessage(id="r", text="again", force=True))

    assert transport.sent[-1].type == "stream_end"
    backend.runtime.spawned[0].close()


async def test_an_armed_plan_wraps_the_next_prompt_once():
    backend = _Backend(runtime=_Runtime())
    backend.plan_armed = True
    dispatcher, transport, _, _ = _dispatcher(backend)

    await dispatcher.dispatch(msg.UserMessage(id="1", text="add caching"))
    await dispatcher.dispatch(msg.UserMessage(id="2", text="and tests"))

    first, second = (args[0] for name, args in backend.calls if name == "run_message")
    assert first.startswith("<system-context>") and "enter_plan_mode" in first
    assert first.endswith("add caching")
    assert second == "and tests"
    # The bubble shows what the person typed, not the hint.
    assert transport.sent[0].text == "add caching"
    for coro in backend.runtime.spawned:
        coro.close()


async def test_the_session_name_is_pushed_only_when_one_was_made():
    named = _Backend(runtime=_Runtime(), auto_name="Caching work")
    dispatcher, transport, _, _ = _dispatcher(named)
    await dispatcher.dispatch(msg.UserMessage(id="1", text="x"))
    await named.runtime.spawned[0]
    push = transport.sent[-1]
    assert (push.type, push.channel) == ("push_notification", "session_named")
    assert push.payload == {"session_id": "s-1", "name": "Caching work"}

    unnamed = _Backend(runtime=_Runtime())
    dispatcher, transport, _, _ = _dispatcher(unnamed)
    await dispatcher.dispatch(msg.UserMessage(id="1", text="x"))
    before = len(transport.sent)
    await unnamed.runtime.spawned[0]
    assert len(transport.sent) == before


async def test_without_a_runtime_the_naming_runs_in_the_background():
    backend = _Backend(runtime=None, auto_name="Named")
    dispatcher, transport, _, _ = _dispatcher(backend)

    await dispatcher.dispatch(msg.UserMessage(id="1", text="x"))
    await background.drain()

    assert transport.sent[-1].payload["name"] == "Named"


async def test_a_permission_answer_dismisses_the_dialog_everywhere_then_resumes():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.dispatch(
        msg.HITLResponse(id="h", requirement_id="req-1", action="confirm", choice="once")
    )

    assert _types(transport.sent) == ["requirement_resolved", "info", "stream_end"]
    assert transport.sent[0].requirement_id == "req-1"
    assert [m.id for m in transport.sent[1:]] == ["h", "h"]
    assert backend.calls == [("resolve_hitl", ("req-1", "confirm", "once"))]


async def test_a_batch_answer_dismisses_every_dialog_and_resumes_once():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.dispatch(
        msg.HITLResponseBatch(
            id="b",
            decisions=[
                msg.HITLDecision(requirement_id="a", action="confirm"),
                msg.HITLDecision(requirement_id="b", action="reject"),
            ],
        )
    )

    assert _types(transport.sent) == [
        "requirement_resolved",
        "requirement_resolved",
        "info",
        "stream_end",
    ]
    assert backend.calls == [("resolve_hitl_batch", ["a", "b"])]
    assert transport.sent[2].id == "b"


async def test_unstamped_permission_answers_stream_without_an_id():
    dispatcher, transport, _, _ = _dispatcher()

    await dispatcher.dispatch(msg.HITLResponse(requirement_id="r", action="reject"))
    await dispatcher.dispatch(msg.HITLResponseBatch(decisions=[]))

    assert not any(m.id for m in transport.sent)


async def test_requests_are_answered_under_their_own_id():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.dispatch(msg.Command(id="c", text="/help"))
    await dispatcher.dispatch(msg.SessionList(id="l"))
    await dispatcher.dispatch(msg.SessionSwitch(id="s", session_id="old"))
    await dispatcher.dispatch(msg.ModelSwitch(id="m", model_name="igni-1.0-4b"))
    await dispatcher.dispatch(msg.MCPToggle(id="t", server_name="git", connect=False))

    assert [m.id for m in transport.sent] == ["c", "l", "s", "m", "t"]
    assert [m.text for m in transport.sent] == [
        "ran /help",
        "sessions",
        "now old",
        "model igni-1.0-4b",
        "toggled",
    ]
    assert [name for name, _ in backend.calls] == [
        "handle_command",
        "list_sessions",
        "switch_session",
        "switch_model",
        "toggle_mcp",
    ]
    assert backend.calls[-1] == ("toggle_mcp", ("git", False))


async def test_a_queued_message_waits_for_the_next_turn_and_shows_as_queued():
    dispatcher, transport, queue, backend = _dispatcher()

    await dispatcher.dispatch(msg.QueueMessage(text="also this", client_id="tab"))

    assert queue == ["also this"]
    (echo,) = transport.sent
    assert (echo.text, echo.client_id, echo.queued) == ("also this", "tab", True)
    assert backend.calls == []


async def test_typing_is_relayed_untouched():
    dispatcher, transport, _, _ = _dispatcher()
    typing = msg.Typing(text="half a thou", client_id="tab")

    await dispatcher.dispatch(typing)

    assert transport.sent == [typing]


async def test_cancel_stops_the_run_and_cancel_login_stops_a_login():
    login = _Login()
    dispatcher, transport, _, backend = _dispatcher(login=login)

    await dispatcher.dispatch(msg.Cancel())
    await dispatcher.dispatch(msg.CancelLogin())

    assert backend.cancelled == 1
    assert login.cancelled == 1
    assert transport.sent == []


async def test_cancel_login_on_a_runtime_without_login_is_harmless():
    dispatcher, transport, _, _ = _dispatcher(login=None)

    await dispatcher.dispatch(msg.CancelLogin())

    assert transport.sent == []


async def test_an_unknown_message_is_logged_not_raised(caplog):
    dispatcher, transport, _, _ = _dispatcher()

    await dispatcher.dispatch(msg.Shutdown(id="x"))

    assert transport.sent == []
    assert "Unknown FE message type: Shutdown" in caplog.text


# ── An extension's messages ───────────────────────────────────────


async def test_an_extension_s_message_is_labelled_as_its_own_and_runs_a_turn():
    dispatcher, transport, _, backend = _dispatcher()

    await dispatcher.post_from_extension("acme.tickets", "Ticket 4 closed", {}, "svc-1")

    echo = transport.sent[0]
    assert (echo.text, echo.client_id, echo.extension_id, echo.service_client_id) == (
        "Ticket 4 closed",
        "",
        "acme.tickets",
        "svc-1",
    )
    ((name, (agent_text, _media)),) = backend.calls
    assert name == "run_message"
    assert agent_text == ExtensionMessage.wrap("acme.tickets", "Ticket 4 closed")
    assert "not typed by the person" in agent_text
    assert '<extension-message from="acme.tickets">' in agent_text
    assert transport.sent[-1].type == "stream_end"
    backend.runtime.spawned[0].close()


async def test_an_extension_s_queued_message_is_wrapped_for_the_agent():
    dispatcher, transport, queue, _ = _dispatcher()

    await dispatcher.queue_from_extension("acme.tickets", "later")

    assert queue == [ExtensionMessage.wrap("acme.tickets", "later")]
    (echo,) = transport.sent
    assert (echo.queued, echo.extension_id, echo.text) == (True, "acme.tickets", "later")


# ── RPC result shapes ─────────────────────────────────────────────


class _Thing(BaseModel):
    n: int = 3


class _Opaque:
    def __str__(self) -> str:
        return "opaque!"


async def test_an_rpc_answers_whatever_shape_its_handler_returns():
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    future.set_result("from a future")
    table = {
        "sync": lambda args: {"echo": args["v"], "flags": (True, None, 1.5)},
        "model": lambda args: _Thing(),
        "opaque": lambda args: _Opaque(),
        "wire": lambda args: msg.Info(text="as a message"),
        "future": lambda args: future,
        "boom": lambda args: (_ for _ in ()).throw(RuntimeError("handler broke")),
    }
    dispatcher, transport, _, _ = _dispatcher(table=table)

    for i, method in enumerate(table):
        await dispatcher.dispatch(msg.RPCRequest(id=f"q{i}", method=method, args={"v": 7}))

    sync, model, opaque, wire, fut, boom = transport.sent
    assert sync.result == {"echo": 7, "flags": [True, None, 1.5]}
    assert sync.result["flags"][0] is True
    assert model.result == {"n": 3}
    assert opaque.result == "opaque!"
    assert (wire.type, wire.text, wire.id) == ("info", "as a message", "q3")
    assert fut.result == "from a future"
    assert (boom.error, boom.id) == ("handler broke", "q5")


class _Extensions:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def dispatch(self, *, caller: Any, method: str, args: dict) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


async def test_an_extension_call_that_breaks_fails_with_the_reason():
    dispatcher, transport, _, _ = _dispatcher(extensions=_Extensions(ValueError("service down")))

    await dispatcher.dispatch(msg.RPCRequest(id="e", method="ext.acme.call", caller="acme"))

    (reply,) = transport.sent
    assert (reply.id, reply.error) == ("e", "service down")


async def test_an_extension_call_that_is_refused_fails_with_the_refusal():
    refusal = ExtensionRefused("acme may not call that")
    dispatcher, transport, _, _ = _dispatcher(extensions=_Extensions(refusal))

    await dispatcher.dispatch(msg.RPCRequest(id="e", method="ext.acme.call", caller="acme"))

    assert transport.sent[0].error == "acme may not call that"
