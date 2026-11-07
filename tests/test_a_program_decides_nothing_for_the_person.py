"""A program decides nothing for the person; only igni's window does.

The backend's WebSocket takes a token. igni's window offered the launch
secret, and so did every program attaching through the lockfile, which
carried that same secret. The agent runs as the person and can read the
lockfile, so it could connect, answer its own approval requests, switch
approvals off, or speak for an extension.

Now the token a connection offers decides its standing:

* the launch secret — held by the launcher, never on disk — makes it
  igni's window, which may do everything;
* the lockfile's token, a different one, makes it a program: it runs
  sessions, but answers no approval request, speaks for no extension and
  loosens no rule;
* a token the window mints on the person's request makes it an approver:
  a program that may also answer approval requests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request

from ember_code.backend import standing_policy
from ember_code.backend.message_dispatcher import MessageDispatcher
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod
from ember_code.protocol.schemas.envelope import Standing
from ember_code.transport.websocket import WebSocketServerTransport
from ember_code.transport.ws_auth import SUBPROTOCOL, TOKEN_PROTOCOL_PREFIX, HandshakeGuard
from tests.host_model_helpers import HostRig

SECRET = "the-launch-secret"


def _offering(token: str) -> Request:
    headers = Headers()
    headers["Sec-WebSocket-Protocol"] = f"{SUBPROTOCOL}, {TOKEN_PROTOCOL_PREFIX}{token}"
    return Request(path="/", headers=headers)


# ── The handshake decides the standing ────────────────────────────


def test_the_launch_secret_makes_a_connection_igni_s_window():
    assert HandshakeGuard(SECRET).standing(_offering(SECRET)) is Standing.WINDOW


def test_the_lockfile_s_token_is_a_program_s_and_never_the_launch_secret():
    guard = HandshakeGuard(SECRET)
    assert guard.program_token and guard.program_token != SECRET
    assert guard.standing(_offering(guard.program_token)) is Standing.PROGRAM


def test_a_minted_token_makes_an_approver_until_it_is_revoked():
    guard = HandshakeGuard(SECRET)
    token = guard.mint_approver_token()
    assert token and token not in (SECRET, guard.program_token)
    assert guard.standing(_offering(token)) is Standing.APPROVER
    assert guard.revoke_approver_tokens() == 1
    assert guard.standing(_offering(token)) is None
    assert guard.refusal(_offering(token)) == "wrong token"


def test_each_minted_token_is_new():
    guard = HandshakeGuard(SECRET)
    assert guard.mint_approver_token() != guard.mint_approver_token()


def test_without_a_token_every_connection_is_a_window_and_nothing_is_minted():
    guard = HandshakeGuard(None)
    assert guard.program_token is None
    assert guard.mint_approver_token() is None
    assert guard.standing(Request(path="/", headers=Headers())) is Standing.WINDOW


# ── The transport stamps it on every message ──────────────────────


async def _first_message(transport: WebSocketServerTransport, token: str) -> msg.Message:
    socket = await connect(
        f"ws://127.0.0.1:{transport.port}",
        subprotocols=[SUBPROTOCOL, f"{TOKEN_PROTOCOL_PREFIX}{token}"],  # type: ignore[list-item]
    )
    try:
        await asyncio.wait_for(socket.recv(), 5)  # welcome
        raw = json.loads(msg.HITLResponse(requirement_id="r", action="approve").model_dump_json())
        raw["_standing"] = "window"  # a client claiming a standing is nothing
        await socket.send(json.dumps(raw))
        async for message in transport.receive():
            return message
    finally:
        await socket.close()
    raise AssertionError("nothing arrived")


async def test_a_message_carries_the_standing_its_connection_was_given():
    transport = WebSocketServerTransport(port=0, token=SECRET)
    await transport.start()
    try:
        assert transport.program_token is not None
        from_a_program = await _first_message(transport, transport.program_token)
        assert from_a_program.standing is Standing.PROGRAM
        from_the_window = await _first_message(transport, SECRET)
        assert from_the_window.standing is Standing.WINDOW
        approver = transport.mint_approver_token()
        assert approver is not None
        assert (await _first_message(transport, approver)).standing is Standing.APPROVER
    finally:
        await transport.close()


async def test_revoking_drops_the_approvers_connected_and_their_token_no_longer_connects():
    transport = WebSocketServerTransport(port=0, token=SECRET)
    await transport.start()
    try:
        token = transport.mint_approver_token()
        approver = await connect(
            f"ws://127.0.0.1:{transport.port}",
            subprotocols=[SUBPROTOCOL, f"{TOKEN_PROTOCOL_PREFIX}{token}"],  # type: ignore[list-item]
        )
        window = await connect(
            f"ws://127.0.0.1:{transport.port}",
            subprotocols=[SUBPROTOCOL, f"{TOKEN_PROTOCOL_PREFIX}{SECRET}"],  # type: ignore[list-item]
        )
        await asyncio.wait_for(approver.recv(), 5)
        await asyncio.wait_for(window.recv(), 5)
        assert await transport.revoke_approver_tokens() == 1
        await asyncio.wait_for(approver.wait_closed(), 5)
        assert approver.close_code == 1008
        assert window.close_code is None
        await window.close()
        with pytest.raises(InvalidStatus):
            await connect(
                f"ws://127.0.0.1:{transport.port}",
                subprotocols=[SUBPROTOCOL, f"{TOKEN_PROTOCOL_PREFIX}{token}"],  # type: ignore[list-item]
            )
    finally:
        await transport.close()


# ── What each standing may do ─────────────────────────────────────


def _from(standing: Standing, message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", standing)
    return message


def _approval() -> msg.Message:
    return msg.HITLResponse(requirement_id="r", action="approve")


def _batch() -> msg.Message:
    return msg.HITLResponseBatch(decisions=[])


@pytest.mark.parametrize("make", [_approval, _batch], ids=["one", "batch"])
def test_a_program_answers_no_approval_request(make: Any):
    assert standing_policy.refusal(_from(Standing.PROGRAM, make()))
    assert standing_policy.refusal(_from(Standing.APPROVER, make())) is None
    assert standing_policy.refusal(_from(Standing.WINDOW, make())) is None


@pytest.mark.parametrize(
    "message",
    [
        msg.HITLResponse(requirement_id="r", action="confirm", choice="always"),
        msg.HITLResponse(requirement_id="r", action="reject", choice="similar"),
        msg.HITLResponseBatch(
            decisions=[msg.HITLDecision(requirement_id="r", action="confirm", choice="always")]
        ),
        msg.HITLResponseBatch(
            decisions=[
                msg.HITLDecision(
                    requirement_id="r", action="confirm", set_permission_mode="bypassPermissions"
                )
            ]
        ),
    ],
    ids=["always", "similar", "batch-always", "batch-bypass"],
)
def test_an_approver_answers_once_and_loosens_no_rule(message: msg.Message):
    assert standing_policy.refusal(_from(Standing.APPROVER, message))
    assert standing_policy.refusal(_from(Standing.WINDOW, message)) is None


def test_an_approver_answers_this_once():
    once = msg.HITLResponseBatch(
        decisions=[msg.HITLDecision(requirement_id="r", action="confirm", choice="once")]
    )
    assert standing_policy.refusal(_from(Standing.APPROVER, once)) is None


def test_a_program_approves_no_plan_but_an_approver_does():
    def plan() -> msg.Message:
        return msg.RPCRequest(id="p", method=RpcMethod.APPROVE_PLAN)

    assert standing_policy.refusal(_from(Standing.PROGRAM, plan()))
    assert standing_policy.refusal(_from(Standing.APPROVER, plan())) is None


@pytest.mark.parametrize("method", sorted(standing_policy.WINDOW_ONLY_RPCS))
def test_only_the_window_loosens_a_rule(method: str):
    for standing in (Standing.PROGRAM, Standing.APPROVER):
        assert standing_policy.refusal(_from(standing, msg.RPCRequest(id="x", method=method)))
    assert (
        standing_policy.refusal(_from(Standing.WINDOW, msg.RPCRequest(id="x", method=method)))
        is None
    )


@pytest.mark.parametrize(
    "text", ["/bypass", "/accept", "/BYPASS on", "bypass", "/plugin install x"]
)
def test_only_the_window_switches_approvals_off(text: str):
    assert standing_policy.refusal(_from(Standing.APPROVER, msg.Command(text=text)))
    assert standing_policy.refusal(_from(Standing.WINDOW, msg.Command(text=text))) is None


def test_a_program_speaks_for_no_extension():
    request = msg.RPCRequest(id="x", method="ext.acme.reviewer.search", caller="acme.reviewer")
    assert standing_policy.refusal(_from(Standing.APPROVER, request))


@pytest.mark.parametrize(
    "message",
    [
        msg.UserMessage(text="fix the tests"),
        msg.Command(text="/plan"),
        msg.RPCRequest(id="s", method=RpcMethod.GET_STATUS),
        msg.RPCRequest(id="c", method=RpcMethod.CANCEL_RUN),
        msg.RPCRequest(id="d", method=RpcMethod.DISMISS_PLAN),
    ],
    ids=["prompt", "plan-mode", "status", "cancel", "dismiss-plan"],
)
def test_a_program_still_runs_sessions(message: msg.Message):
    assert standing_policy.refusal(_from(Standing.PROGRAM, message)) is None


# ── The backend refuses before any handler ────────────────────────


async def test_a_program_s_approval_is_refused_before_any_handler(tmp_path: Path, monkeypatch):
    rig = await HostRig(tmp_path).start()
    reached: list[str] = []

    async def resolve(self: Any, message: Any, req_id: str) -> None:
        reached.append(message.requirement_id)

    monkeypatch.setattr(MessageDispatcher, "_on_hitl_response", resolve)
    try:
        assert rig.orchestrator is not None
        await rig.orchestrator.dispatch(
            _from(Standing.PROGRAM, msg.HITLResponse(id="a", requirement_id="r1", action="approve"))
        )
        await rig.orchestrator.dispatch(
            _from(
                Standing.APPROVER, msg.HITLResponse(id="b", requirement_id="r2", action="approve")
            )
        )
        assert reached == ["r2"]
        refused = [e for e in rig.windows.of(msg.Error) if e.id == "a"]
        assert refused and "approver token" in refused[0].text
        assert refused[0].to_client == "ws-1"
        # The stream the approval would have resumed ends, for that client alone.
        ended = [e for e in rig.windows.of(msg.StreamEnd) if e.id == "a"]
        assert ended and ended[0].to_client == "ws-1"
    finally:
        await rig.stop()


async def test_a_program_s_bypass_is_refused_as_a_command_result(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        await rig.orchestrator.dispatch(
            _from(Standing.PROGRAM, msg.Command(id="c", text="/bypass"))
        )
        results = [r for r in rig.windows.of(msg.CommandResult) if r.id == "c"]
        assert results and results[0].kind == "error"
        assert "only igni's window" in results[0].content
    finally:
        await rig.stop()


class _Tokens:
    """The transport's half, over a bare guard."""

    def __init__(self, guard: HandshakeGuard) -> None:
        self.guard = guard

    def mint_approver_token(self) -> str | None:
        return self.guard.mint_approver_token()

    async def revoke_approver_tokens(self) -> int:
        return self.guard.revoke_approver_tokens()


async def test_the_window_mints_an_approver_token_and_only_it_hears_it(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    guard = HandshakeGuard(SECRET)
    try:
        assert rig.orchestrator is not None
        rig.orchestrator.attach_approver_tokens(_Tokens(guard))
        await rig.orchestrator.dispatch(
            _from(Standing.PROGRAM, msg.RPCRequest(id="p", method=RpcMethod.MINT_APPROVER_TOKEN))
        )
        window = msg.RPCRequest(id="w", method=RpcMethod.MINT_APPROVER_TOKEN)
        window.mark_from("ws-window", Standing.WINDOW)
        await rig.orchestrator.dispatch(window)
        answers = {m.id: m for m in rig.windows.of(msg.RPCResponse)}
        assert "only igni's window" in (answers["p"].error or "")
        token = answers["w"].result["token"]
        assert answers["w"].to_client == "ws-window"
        assert guard.standing(_offering(token)) is Standing.APPROVER

        revoke = msg.RPCRequest(id="r", method=RpcMethod.REVOKE_APPROVER_TOKENS)
        revoke.mark_from("ws-window", Standing.WINDOW)
        await rig.orchestrator.dispatch(revoke)
        answers = {m.id: m for m in rig.windows.of(msg.RPCResponse)}
        assert answers["r"].result == {"revoked": 1}
        assert guard.standing(_offering(token)) is None
    finally:
        await rig.stop()
