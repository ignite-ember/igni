"""A real MCP server, for the end-to-end install test.

Small on purpose: three tools, one of which fails, so the test can
prove a tool result reaches an extension's UI *and* that a failure
reaches it as a sentence rather than as a crash.

Run by :mod:`ember_code.extensions.installer` as a subprocess over
stdio, exactly the way a development extension is.
"""

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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
def explode() -> str:
    """Always fails, so the failure path is covered too."""
    raise RuntimeError("the scanner fell over")


if __name__ == "__main__":
    server.run()
    sys.exit(0)
