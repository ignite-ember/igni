"""HITLHandler — handles Human-in-the-Loop requirements.

Pure FE — no core imports. Permission checks go through BackendClient RPC.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import PermissionDialog
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp
    from ember_code.frontend.tui.conversation_view import ConversationView

_log = logging.getLogger("ember_code.llm_calls")


class HITLHandler:
    """Handles Human-in-the-Loop requirements: confirmations and user input.

    Permission checks and rule persistence happen via RPC to the BE.
    """

    def __init__(
        self,
        app: EmberApp,
        conversation: ConversationView,
    ):
        self._app = app
        self._conversation = conversation
        # Session-level one-time approvals: set of "ToolName(args_str)"
        self._session_approvals: set[str] = set()

    async def handle_protocol(self, req) -> tuple[str, str]:
        """Handle a protocol HITLRequest — show dialog, return (action, choice).

        Returns ("confirm", choice) or ("reject", "").
        """
        tool_name = req.friendly_name or req.tool_name
        tool_args = req.tool_args or {}
        func_name = req.tool_name
        # Build a path-aware display name when the request came from a
        # spawned specialist. Empty path means the main agent — show the
        # tool name alone, same as before. Non-empty path shows the
        # chain so the user knows which sub-agent is asking.
        agent_path = list(getattr(req, "agent_path", []) or [])
        display_name = " → ".join(agent_path + [tool_name]) if agent_path else tool_name
        _log.info(
            "hitl_handler: enter req_id=%s tool=%s path=%s",
            req.requirement_id,
            tool_name,
            agent_path,
        )

        # Check permission rules via BE RPC
        backend = self._app.backend
        try:
            level = await backend._rpc(
                "check_permission",
                tool_name=tool_name,
                func_name=func_name,
                tool_args=tool_args,
            )
        except Exception as exc:
            _log.info("hitl_handler: check_permission rpc failed (%s) — defaulting to ask", exc)
            level = "ask"
        _log.info("hitl_handler: check_permission returned level=%s", level)

        if level == "allow":
            _log.info("hitl_handler: auto-approving via existing rule")
            return "confirm", "once"
        if level == "deny":
            _log.info("hitl_handler: auto-denying via existing rule")
            return "reject", ""

        # Check session approvals
        args_str = _format_args_short(tool_args)
        session_key = f"{tool_name}({args_str})"
        if session_key in self._session_approvals:
            _log.info("hitl_handler: auto-approving via session approval")
            return "confirm", "once"

        # Show dialog. For sub-agent requests, prefix the dialog title
        # with the dispatch chain so the user knows which specialist
        # is asking — not just the tool.
        details = _format_args_detail(tool_args)
        dialog = PermissionDialog(
            tool_name=display_name,
            details=details,
        )
        _log.info("hitl_handler: about to mount dialog")
        await self._app.mount(dialog)
        _log.info("hitl_handler: dialog mounted, focusing")
        dialog.focus()
        _log.info("hitl_handler: awaiting user decision")

        approved = await dialog.wait_for_decision()
        _log.info(
            "hitl_handler: dialog returned approved=%s choice=%s",
            approved,
            getattr(dialog, "last_choice", None),
        )
        if not approved:
            # Save deny rule via BE RPC
            rule = _build_rule(tool_name, tool_args)
            with contextlib.suppress(Exception):
                await backend._rpc(RpcMethod.SAVE_PERMISSION_RULE, rule=rule, level="deny")
            self._conversation.append_info(f"Saved rule: deny {rule}")
            return "reject", "deny"

        choice = dialog.last_choice
        if choice == "once":
            self._session_approvals.add(session_key)
        elif choice == "always":
            rule = _build_rule(tool_name, tool_args)
            with contextlib.suppress(Exception):
                await backend._rpc(RpcMethod.SAVE_PERMISSION_RULE, rule=rule, level="allow")
            self._conversation.append_info(f"Saved rule: allow {rule}")
        elif choice == "similar":
            rule = _build_pattern_rule(tool_name, tool_args)
            with contextlib.suppress(Exception):
                await backend._rpc(RpcMethod.SAVE_PERMISSION_RULE, rule=rule, level="allow")
            self._conversation.append_info(f"Saved rule: allow {rule}")

        return "confirm", choice


def _format_args_short(args: dict) -> str:
    """Short args representation for session key."""
    if "args" in args and isinstance(args["args"], list):
        return " ".join(str(a) for a in args["args"])
    for key in ("path", "file_path", "url", "query"):
        if key in args:
            return str(args[key])
    return str(args)[:100]


def _format_args_detail(args: dict) -> str:
    """Full args for the permission dialog display."""
    if "args" in args and isinstance(args["args"], list):
        cmd = " ".join(str(a) for a in args["args"])
        return f"$ {cmd}"
    for key in ("path", "file_path", "file_name"):
        if key in args:
            return str(args[key])
    parts = []
    for k, v in args.items():
        parts.append(f"{k}: {v}")
    return "\n".join(parts)


def _build_rule(tool_name: str, tool_args: dict) -> str:
    """Build a specific rule string from a tool call."""
    args_str = _format_args_short(tool_args)
    if args_str:
        return f"{tool_name}({args_str})"
    return tool_name


def _build_pattern_rule(tool_name: str, tool_args: dict) -> str:
    """Build a pattern rule from a tool call."""
    from pathlib import Path

    if "args" in tool_args and isinstance(tool_args["args"], list):
        cmd = tool_args["args"]
        if cmd:
            return f"{tool_name}({cmd[0]}:*)"
    for key in ("path", "file_path"):
        if key in tool_args:
            parent = str(Path(str(tool_args[key])).parent)
            if parent and parent != ".":
                return f"{tool_name}(path:{parent}/*)"
    if "url" in tool_args:
        from urllib.parse import urlparse

        domain = urlparse(str(tool_args["url"])).netloc
        if domain:
            return f"{tool_name}(domain:{domain})"
    return tool_name
