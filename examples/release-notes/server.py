"""The Release Notes backend.

An MCP server. igni calls its tools as ``ext.acme.notes.<tool>`` when
this extension's UI asks, and only the ones ``permissions.rpc``
grants.

State is in memory and resets when the process does, which is right
for an example: a real one would write to the workspace it was
granted (``IGNI_WORKSPACE``, here scoped to ``CHANGELOG.md`` and
``docs/**``). What is *not* simplified is the revision check — that
is the part worth copying, because a document is the one shape where
two people editing at once is normal rather than exceptional, and
"last write wins" there loses somebody's afternoon with no error.
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("acme-notes")

Path("server.pid").write_text(str(os.getpid()))

#: version → note. ``revision`` changes on every write and the UI
#: sends back the one it loaded.
NOTES: dict[str, dict] = {
    "2.4.0": {
        "version": "2.4.0",
        "body": (
            "# 2.4.0\n\n"
            "Extensions can now be installed from the command line.\n\n"
            "- `igni ext init` scaffolds one that runs as written\n"
            "- `igni ext doctor` says what is wrong before you install\n\n"
            "```\nigni ext install ./my-extension\n```\n"
        ),
        "revision": "r1",
    },
    "2.3.1": {
        "version": "2.3.1",
        "body": "# 2.3.1\n\nA dead extension stops claiming to be READY.\n",
        "revision": "r1",
    },
}

RELEASES = [
    {"version": "2.4.0", "state": "draft", "publishedAt": None},
    {"version": "2.3.1", "state": "published", "publishedAt": "2026-10-20T09:00:00Z"},
]

COMMENTS = [
    {
        "id": "c1",
        "version": "2.4.0",
        "author": "Maya Okonkwo",
        "body": "Worth saying the scaffold runs without edits — that is the point.",
        "resolved": False,
    }
]

_revision = 1


@server.tool()
def releases() -> str:
    """Every release, newest first."""
    return json.dumps(RELEASES)


@server.tool()
def note(version: str) -> str:
    """One release's note."""
    found = NOTES.get(version)
    if found is None:
        raise ValueError(f"no note for {version}")
    return json.dumps(found)


@server.tool()
def comments(version: str) -> str:
    """Comments on one release."""
    return json.dumps([c for c in COMMENTS if c["version"] == version])


@server.tool()
def save(version: str, body: str, revision: str) -> str:
    """Save a draft, refusing to overwrite somebody else's.

    Answers ``{"conflict": "<their revision>"}`` rather than failing.
    A conflict is an outcome: the UI has to keep the user's text
    either way, and an exception that also carries data is a worse
    shape than a union.
    """
    global _revision
    found = NOTES.get(version)
    if found is None:
        raise ValueError(f"no note for {version}")
    if found["revision"] != revision:
        return json.dumps({"conflict": found["revision"]})

    _revision += 1
    found["body"] = body
    found["revision"] = f"r{_revision}"
    return json.dumps(found)


@server.tool()
def publish(version: str) -> str:
    """Publish a draft."""
    for release in RELEASES:
        if release["version"] == version:
            release["state"] = "published"
            return json.dumps(release)
    raise ValueError(f"no release {version}")


if __name__ == "__main__":
    server.run()
