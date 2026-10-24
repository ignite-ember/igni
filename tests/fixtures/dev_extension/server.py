"""A real MCP server, for the end-to-end install test.

Small on purpose: a handful of tools, one of which fails, so the
test can prove a tool result reaches an extension's UI *and* that a
failure reaches it as a sentence rather than as a crash.

It also implements ``igni_on_event``, the reserved sink, and writes
what it is told to ``events.jsonl`` — which is how a test proves a
host event reached a *service* rather than only a frame.

Run by :mod:`ember_code.extensions.installer` as a subprocess over
stdio, exactly the way a development extension is.
"""

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

server = FastMCP("acme-scanner")

# Its own pid, beside itself. The health test has to kill this
# process to prove a crash is noticed, and finding it by scanning the
# process tree was both fragile and a dependency; a service that
# writes down where it is costs one line.
#
# ``cwd`` is the bundle, because that is what the installer sets.
Path("server.pid").write_text(str(os.getpid()))


@server.tool()
def list() -> str:
    """Every open violation."""
    return json.dumps(
        [
            {
                "id": "v1",
                "rule": "no-plaintext-secrets",
                "severity": "critical",
                "path": "src/config.py",
                "line": 12,
            }
        ]
    )


@server.tool()
def status() -> str:
    """What the scan is doing."""
    return json.dumps({"state": "idle", "lastFinished": "2026-10-23T09:00:00Z"})


@server.tool()
def policy_summary() -> str:
    """The text that fills ``{{ACME_POLICY_SUMMARY}}`` in a prompt.

    A plain string, not JSON: a prompt fragment is prose for a model
    to read, and wrapping it in a structure would only mean unwrapping
    it before substitution.
    """
    return "Acme policy: no plaintext secrets; all egress declared."


@server.tool()
def compliance(args: str = "") -> str:
    """What ``/compliance`` runs.

    A slash command is dispatched to the tool with the command's own
    name, and the rest of the line arrives as ``args``. One rule, and
    the same one ``ext.<id>.<method>`` already uses.
    """
    return f"Compliance report{f' for {args}' if args else ''}: 1 open violation."


@server.tool()
def igni_on_event(topic: str, payload: dict) -> str:
    """The reserved sink: igni telling this service something.

    Appended to a file rather than held in memory, because the test
    that reads it is in another process — the whole point of this
    fixture is that nothing here is a stub.
    """
    with Path("events.jsonl").open("a") as handle:
        handle.write(json.dumps({"topic": topic, "payload": payload}) + "\n")
    return json.dumps({"ok": True})


class IgniNotify(BaseModel):
    """``notifications/igni/notify``, which the MCP SDK has no type for.

    ``send_notification`` only dumps what it is given, so a model with
    the method and the params is all it takes to send one.
    """

    method: str = "notifications/igni/notify"
    params: dict


@server.tool()
async def notify_me(ctx: Context, params: dict) -> str:
    """Tell the host something, exactly as an extension's ``app.notify`` would.

    The params are the test's, unchecked, so a test can send a notice
    the host must refuse as easily as one it must keep.
    """
    await ctx.session.send_notification(IgniNotify(params=params))  # type: ignore[arg-type]
    return json.dumps({"sent": True})


@server.tool()
def explode() -> str:
    """Always fails, so the failure path is covered too."""
    raise RuntimeError("the scanner fell over")


if __name__ == "__main__":
    server.run()
    sys.exit(0)
