"""Ticket lookup: two tools, a hook, and a page that calls one of them."""

import json
from pathlib import Path

from igni_extension import Hooks, IgniExtension, NotSignedIn, ToolHints
from igni_extension.hooks import PostToolUse

ext = IgniExtension("acme.tickets")

TICKETS = {
    "T-1001": {"id": "T-1001", "title": "VPN drops every hour", "status": "open", "owner": "ada"},
    "T-1002": {
        "id": "T-1002",
        "title": "Printer on 3rd floor offline",
        "status": "closed",
        "owner": "grace",
    },
}


@ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
async def lookup_ticket(ticket_id: str) -> str:
    """Look up a support ticket by id (e.g. T-1001). Returns JSON."""
    ticket = TICKETS.get(ticket_id.upper())
    if ticket is None:
        return json.dumps({"error": f"no ticket {ticket_id}", "known": sorted(TICKETS)})
    return json.dumps(ticket)


@ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=True))
async def ticket_from_service(ticket_id: str) -> str:
    """Look a ticket up on the Ticket lookup service, as the signed-in person."""
    try:
        response = await ext.service.get(f"/v1/tickets/{ticket_id}")
    except NotSignedIn:
        return "Sign in to igni to reach the ticket service."
    response.raise_for_status()
    return response.text


@ext.hook(Hooks.POST_TOOL_USE)
async def audit(call: PostToolUse) -> None:
    """Record every tool the agent ran in this extension's data directory."""
    data_dir: Path | None = ext.environment.data_dir  # None when igni did not start it
    if data_dir is None:
        return
    with (data_dir / "tool-audit.log").open("a") as f:
        f.write(f"{call.tool_name}\n")


def main() -> None:
    ext.run()
