"""``igni ext doctor`` starts the process and does the MCP round trip.

It used to read the manifest and stop, and EXTENSIBILITY.md said it
checked the extension: a bundle whose process died on import, or whose
tools were not the ones its manifest declared, passed. Now it starts
the process the way igni would, asks for its tools with a timeout, and
says what each tool will be called where.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext

HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

SERVER = """
from mcp.server.fastmcp import FastMCP

server = FastMCP("t")


@server.tool()
def lookup_ticket(ticket_id: str) -> str:
    return ticket_id


@server.tool()
def secret_tool() -> str:
    return "x"


if __name__ == "__main__":
    server.run()
"""


def _bundle(tmp_path: Path, *, tools=None, rpc=None, server: str = SERVER) -> Path:
    bundle = tmp_path / "b"
    bundle.mkdir()
    manifest = {
        "id": "acme.tickets",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": [sys.executable, "server.py"]},
        "permissions": {
            "tools": tools
            if tools is not None
            else [{"name": "acme.tickets.lookup_ticket", "annotations": HINTS}],
            "rpc": rpc if rpc is not None else ["ext.acme.tickets.lookup_ticket"],
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    (bundle / "server.py").write_text(server)
    return bundle


def _doctor(*args: str):
    return CliRunner().invoke(ext, ["doctor", *args])


def test_doctor_starts_it_and_lists_what_each_tool_is_called(tmp_path):
    result = _doctor(str(_bundle(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "started it and asked for its tools" in result.output
    # The four names one tool goes by, side by side.
    assert "lookup_ticket" in result.output
    assert "acme.tickets.lookup_ticket" in result.output
    assert "ext.acme.tickets.lookup_ticket" in result.output


def test_doctor_names_what_the_model_is_sent(tmp_path):
    """The registered name has ':' and '.', which no hosted provider
    accepts, so igni renames it at the provider boundary. A hook or a
    permission rule sees the registered name; the model, and a training
    corpus, see the other one. Both are printed, so neither is a guess."""
    result = _doctor(str(_bundle(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "registry mcp_ext:acme.tickets_lookup_ticket" in result.output
    assert "model    mcp_ext_acme_tickets_lookup_ticket" in result.output


def test_a_tool_the_manifest_does_not_declare_is_named(tmp_path):
    result = _doctor(str(_bundle(tmp_path)))
    assert "secret_tool" in result.output
    assert "nothing can call it" in result.output


def test_a_declared_tool_the_process_does_not_offer_is_a_problem(tmp_path):
    tools = [
        {"name": "acme.tickets.lookup_ticket", "annotations": HINTS},
        {"name": "acme.tickets.gone", "annotations": HINTS},
    ]
    result = _doctor(str(_bundle(tmp_path, tools=tools)))
    assert result.exit_code != 0
    assert "acme.tickets.gone" in result.output
    assert "does not offer" in result.output


def test_a_process_that_dies_at_start_says_why(tmp_path):
    server = "import sys\nprint('cannot find the ticket database', file=sys.stderr)\nsys.exit(3)\n"
    result = _doctor(str(_bundle(tmp_path, server=server)))
    assert result.exit_code != 0
    assert "cannot find the ticket database" in result.output


def test_a_process_that_never_answers_times_out(tmp_path):
    server = "import time\ntime.sleep(60)\n"
    result = _doctor(str(_bundle(tmp_path, server=server)), "--timeout", "2")
    assert result.exit_code != 0
    assert "did not answer" in result.output


def test_no_start_reads_only(tmp_path):
    server = "import sys\nsys.exit(3)\n"
    result = _doctor(str(_bundle(tmp_path, server=server)), "--no-start")
    assert result.exit_code == 0, result.output
    assert "not started (--no-start)" in result.output


# ── names and grants ───────────────────────────────────────────────


def test_an_unprefixed_tool_name_is_warned_about_and_explained(tmp_path):
    tools = [{"name": "lookup_ticket", "annotations": HINTS}]
    result = _doctor(str(_bundle(tmp_path, tools=tools)), "--no-start")
    assert result.exit_code == 0, result.output
    assert "'lookup_ticket'" in result.output
    assert "acme.tickets.lookup_ticket" in result.output
    assert "warning" in result.output


@pytest.mark.parametrize("grant", ["ext.other.*", "ext.other.lookup", "sessions.list"])
def test_an_rpc_grant_outside_its_namespace_is_warned_about(tmp_path, grant):
    result = _doctor(str(_bundle(tmp_path, rpc=[grant])), "--no-start")
    assert grant in result.output
    assert "ext.acme.tickets." in result.output
    assert "warning" in result.output
