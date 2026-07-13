"""Backwards-compat re-export shim for the conversation content widgets.

Historically this module held every message-rendering widget in
one file (681 LoC). The per-widget split (iters 39–42) moved
each one to its own module. This file remains only so that
existing private-path imports keep working:

    from ember_code.frontend.tui.widgets._messages import ToolCallLiveWidget

Canonical locations:
    - :mod:`_messages_common`        — TOOL_FRIENDLY_NAMES shared consts
    - :mod:`_message_widget`         — MessageWidget
    - :mod:`_streaming_message_widget` — StreamingMessageWidget
    - :mod:`_tool_call_widget`       — ToolCallWidget
    - :mod:`_tool_call_live_widget`  — ToolCallLiveWidget
    - :mod:`_mcp_call_widget`        — MCPCallWidget
    - :mod:`_agent_tree_widget`      — AgentTreeWidget

New code should import from those directly. This shim exists to
keep patches / tests written against the old dotted path working.
"""

from ember_code.frontend.tui.widgets._agent_tree_widget import AgentTreeWidget
from ember_code.frontend.tui.widgets._mcp_call_widget import MCPCallWidget
from ember_code.frontend.tui.widgets._message_widget import MessageWidget
from ember_code.frontend.tui.widgets._messages_common import TOOL_FRIENDLY_NAMES
from ember_code.frontend.tui.widgets._streaming_message_widget import StreamingMessageWidget
from ember_code.frontend.tui.widgets._tool_call_live_widget import ToolCallLiveWidget
from ember_code.frontend.tui.widgets._tool_call_widget import ToolCallWidget

__all__ = [
    "AgentTreeWidget",
    "MCPCallWidget",
    "MessageWidget",
    "StreamingMessageWidget",
    "TOOL_FRIENDLY_NAMES",
    "ToolCallLiveWidget",
    "ToolCallWidget",
]
