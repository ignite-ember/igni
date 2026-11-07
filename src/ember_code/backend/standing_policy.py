"""What a connection may do, by its standing.

A connection's :class:`~ember_code.protocol.schemas.envelope.Standing` is
decided at its handshake by the token it offered (``transport/ws_auth.py``).
igni's window holds the launch secret, which never touches the disk. A
program attaches with the lockfile's token — and so can the agent, which
runs as the person and reads what its tools can. So a program may run
sessions but not decide for the person:

* it answers no approval request (``hitl_response``, ``approve_plan``) —
  unless the window minted it an approver token, and then only for this
  once: no saved rule (``always``, ``similar``), no change of mode;
* it speaks for no extension (``caller``), whose grants and service token
  are the page's igni's window hosts;
* it loosens no rule: no permission mode that skips approvals, no saved
  permission rule, no trusted workspace, no plugin, marketplace or
  extension setting, no shell command outside a run, no change of whom
  igni signs in to;
* it mints no approver token.

An approver is a program in every other respect. What a program may not do
it is told plainly, so the SDK can say why.
"""

from __future__ import annotations

from typing import Any, Protocol

from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod
from ember_code.protocol.schemas.envelope import Standing

__all__ = [
    "APPROVAL_RPCS",
    "ApproverTokens",
    "WINDOW_ONLY_COMMANDS",
    "WINDOW_ONLY_RPCS",
    "refusal",
]


class ApproverTokens(Protocol):
    """Mints and forgets approver tokens: the WebSocket transport."""

    def mint_approver_token(self) -> str | None: ...

    async def revoke_approver_tokens(self) -> int: ...


#: Calls only igni's window makes: each loosens a rule the agent is held to,
#: speaks as a page, or changes whom igni acts for.
WINDOW_ONLY_RPCS: frozenset[str] = frozenset(
    {
        RpcMethod.SAVE_PERMISSION_RULE,
        RpcMethod.SET_WORKSPACE_TRUST,
        RpcMethod.RUN_SHELL,
        RpcMethod.INSTALL_PLUGIN,
        RpcMethod.UPDATE_PLUGIN,
        RpcMethod.REMOVE_PLUGIN,
        RpcMethod.SET_PLUGIN_ENABLED,
        RpcMethod.ADD_MARKETPLACE,
        RpcMethod.REMOVE_MARKETPLACE,
        RpcMethod.SET_EXTENSION_SETTINGS,
        RpcMethod.PROMOTE_EPHEMERAL_AGENT,
        RpcMethod.LOGIN,
        RpcMethod.SET_SERVER,
        RpcMethod.RELOAD_CLOUD_CREDENTIALS,
        RpcMethod.CLEAR_CLOUD_CREDENTIALS,
        RpcMethod.HOST_MODEL,
        RpcMethod.WINDOW_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN,
        RpcMethod.MINT_APPROVER_TOKEN,
        RpcMethod.REVOKE_APPROVER_TOKENS,
    }
)

#: Calls that decide for the person on something the agent asked: the
#: window's, and an approver's.
APPROVAL_RPCS: frozenset[str] = frozenset({RpcMethod.APPROVE_PLAN})

#: Slash commands only igni's window runs: each switches approvals off or
#: changes what igni loads or whom it signs in to.
WINDOW_ONLY_COMMANDS: frozenset[str] = frozenset(
    {"/bypass", "/accept", "/plugin", "/eject", "/login", "/logout"}
)


def _command_name(text: str) -> str:
    words = (text or "").strip().split(maxsplit=1)
    name = words[0].lower() if words else ""
    return name if name.startswith("/") else f"/{name}"


def refusal(message: Any) -> str | None:
    """Why ``message`` is refused from the connection it came on, or ``None``."""
    standing = getattr(message, "standing", Standing.WINDOW)
    if standing is Standing.WINDOW:
        return None
    if isinstance(message, msg.RPCRequest):
        if message.caller:
            return f"{message.method}: only igni's window speaks for an extension"
        if message.method in WINDOW_ONLY_RPCS:
            return f"{message.method}: only igni's window may do this"
        if message.method in APPROVAL_RPCS and standing is not Standing.APPROVER:
            return f"{message.method}: {_NOT_AN_APPROVER}"
        return None
    if isinstance(message, msg.HITLResponse | msg.HITLResponseBatch):
        if standing is not Standing.APPROVER:
            return f"{message.type}: {_NOT_AN_APPROVER}"
        return _beyond_this_once(message)
    if isinstance(message, msg.Command):
        name = _command_name(message.text)
        if name in WINDOW_ONLY_COMMANDS:
            return f"{name}: only igni's window may do this"
    return None


def _beyond_this_once(message: msg.HITLResponse | msg.HITLResponseBatch) -> str | None:
    """Why an approver's answer is refused: it would outlast this one pause.

    An answer can save a permission rule (``always``, ``similar``) or switch
    the session's permission mode before it resumes. Both loosen a rule,
    which only igni's window does; an approver answers this time only.
    """
    rows = message.decisions if isinstance(message, msg.HITLResponseBatch) else [message]
    for row in rows:
        if row.choice not in ("", "once"):
            return f"{message.type}: an approver answers once; only igni's window saves a rule"
        if getattr(row, "set_permission_mode", ""):
            return (
                f"{message.type}: an approver answers once; only igni's window changes "
                "the permission mode"
            )
    return None


_NOT_AN_APPROVER = (
    "a program answers approval requests only with an approver token, "
    "which the person makes in igni's window"
)
