"""FE → BE actions the user (or an FE-side coordinator) can send.

Every previously-free-string field that names a closed set of
values is retyped to its :mod:`.enums` StrEnum member. Wire
strings stay unchanged; producers on the FE side that send raw
JSON like ``{"action": "confirm"}`` still deserialize correctly
because Pydantic accepts either the enum member or its underlying
string.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ember_code.protocol.schemas.envelope import Message

# HITLAction / HITLChoice are referenced by docstrings only —
# the wire uses raw ``str`` for forward-compat. Producers import
# the enums directly from :mod:`.enums` for typed dispatch.


class UserMessage(Message):
    """User sends a chat message.

    ``client_id`` (mirroring) identifies the sending view so the BE's
    ``UserMessageReceived`` broadcast lets every OTHER view paint the
    bubble while the sender skips its own echo. Empty for views that
    predate mirroring — the echo then renders everywhere except
    nowhere, which is harmless for a single view.
    """

    type: Literal["user_message"] = "user_message"
    text: str = ""
    file_contents: dict[str, str] = Field(default_factory=dict)  # path → content
    client_id: str = ""


class QueueMessage(Message):
    """User types while agent is running."""

    type: Literal["queue_message"] = "queue_message"
    text: str = ""
    client_id: str = ""


class HITLResponse(Message):
    """User responded to a permission dialog.

    ``action`` / ``choice`` stay as raw ``str`` on the wire; use
    :class:`HITLAction` / :class:`HITLChoice` members at producer
    callsites for autocompletion + typo-checking. ``StrEnum``
    coerces to the underlying string on assignment.
    """

    type: Literal["hitl_response"] = "hitl_response"
    requirement_id: str = ""
    action: str = ""  # "confirm" | "reject" — see :class:`HITLAction`
    choice: str = ""  # "once" | "always" | "similar" — see :class:`HITLChoice`


class HITLDecision(Message):
    """One row inside a ``HITLResponseBatch``. See :class:`HITLResponse`
    for the ``action`` / ``choice`` typing note."""

    type: Literal["hitl_decision"] = "hitl_decision"
    requirement_id: str = ""
    action: str = ""  # "confirm" | "reject" — see :class:`HITLAction`
    choice: str = ""  # "once" | "always" | "similar" — see :class:`HITLChoice`


class HITLResponseBatch(Message):
    """User responded to *every* requirement in a multi-req pause.

    Agno's ``acontinue_run`` treats requirements not in the resolution
    list as denied, so a per-req resolve loop dropped 7-of-8 calls in
    a batched tool plan. The batch envelope carries every decision in
    one round-trip so the backend can call ``acontinue_run`` exactly
    once with the full set of resolved requirements.
    """

    type: Literal["hitl_response_batch"] = "hitl_response_batch"
    decisions: list[HITLDecision] = []


class Command(Message):
    """Slash command from user."""

    type: Literal["command"] = "command"
    text: str = ""


class Cancel(Message):
    """Cancel current run."""

    type: Literal["cancel"] = "cancel"


class CancelLogin(Message):
    """Cancel an in-progress login flow."""

    type: Literal["cancel_login"] = "cancel_login"


class SessionSwitch(Message):
    """Switch to a different session."""

    type: Literal["session_switch"] = "session_switch"
    session_id: str = ""


class SessionList(Message):
    """Request session list."""

    type: Literal["session_list"] = "session_list"


class ModelSwitch(Message):
    """Switch model."""

    type: Literal["model_switch"] = "model_switch"
    model_name: str = ""


class MCPToggle(Message):
    """Toggle MCP server connection."""

    type: Literal["mcp_toggle"] = "mcp_toggle"
    server_name: str = ""
    connect: bool = True


class Shutdown(Message):
    """Graceful shutdown."""

    type: Literal["shutdown"] = "shutdown"


class StreamEnd(Message):
    """Marks end of a streaming response (run_message, resolve_hitl)."""

    type: Literal["stream_end"] = "stream_end"


__all__ = [
    "UserMessage",
    "QueueMessage",
    "HITLResponse",
    "HITLDecision",
    "HITLResponseBatch",
    "Command",
    "Cancel",
    "CancelLogin",
    "SessionSwitch",
    "SessionList",
    "ModelSwitch",
    "MCPToggle",
    "Shutdown",
    "StreamEnd",
]
