"""One tool, the names it goes by, and the manifest lines it needs — from one place."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import igni_extension
from igni_extension import HostError, HostErrorCode, IgniExtension, ToolHints, ToolNames

READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)
WRITES = ToolHints(read_only=False, destructive=False, idempotent=False, open_world=True)


def tickets() -> IgniExtension:
    ext = IgniExtension("acme.tickets", environ={})

    @ext.tool(hints=READS)
    async def lookup_ticket(ticket_id: str) -> str:
        return ticket_id

    @ext.tool(hints=WRITES, name="close")
    def close_ticket(ticket_id: str) -> str:
        return ticket_id

    return ext


def test_every_name_a_tool_goes_by() -> None:
    names = tickets().tool_names("lookup_ticket")

    assert names == ToolNames(
        tool="lookup_ticket",
        manifest="acme.tickets.lookup_ticket",
        rpc="ext.acme.tickets.lookup_ticket",
        registered="mcp_ext:acme.tickets_lookup_ticket",
    )


def test_a_tool_igni_does_not_have_is_said_so() -> None:
    with pytest.raises(KeyError, match="lookup_tickets"):
        tickets().tool_names("lookup_tickets")


def test_the_decorated_function_knows_its_manifest_name() -> None:
    ext = IgniExtension("acme.tickets", environ={})

    @ext.tool(hints=READS)
    async def lookup_ticket(ticket_id: str) -> str:
        return ticket_id

    assert lookup_ticket.manifest_name == "acme.tickets.lookup_ticket"  # type: ignore[attr-defined]


def test_the_manifest_s_tools_come_from_the_code() -> None:
    assert tickets().manifest_tools() == [
        {
            "name": "acme.tickets.close",
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        },
        {
            "name": "acme.tickets.lookup_ticket",
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
    ]


def test_the_command_prints_them_for_a_module(tmp_path: Path) -> None:
    (tmp_path / "acme_tickets.py").write_text(
        textwrap.dedent(
            """
            from igni_extension import IgniExtension, ToolHints
            ext = IgniExtension("acme.tickets")

            @ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
            async def lookup_ticket(ticket_id: str) -> str:
                return ticket_id

            def main() -> None:
                raise SystemExit("main() must not run")
            """
        )
    )
    src = Path(igni_extension.__file__).parent.parent
    ran = subprocess.run(
        [sys.executable, "-m", "igni_extension.manifest", "acme_tickets"],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": f"{tmp_path}:{src}", "PATH": ""},
    )

    assert ran.returncode == 0, ran.stderr
    printed = json.loads(ran.stdout)
    assert printed["id"] == "acme.tickets"
    assert [tool["name"] for tool in printed["tools"]] == ["acme.tickets.lookup_ticket"]


class TestStartedOutsideIgni:
    """`ext.run()` from a terminal waits for MCP on stdin; it says so instead of hanging."""

    def test_a_terminal_gets_a_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("anyio.run", lambda *args, **kwargs: None)

        tickets().run()

        said = capsys.readouterr().err
        assert "acme.tickets" in said
        assert "igni starts" in said
        assert "HostHarness" in said

    def test_igni_starting_it_gets_none(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr("anyio.run", lambda *args, **kwargs: None)

        tickets().run()

        assert capsys.readouterr().err == ""


def test_a_host_error_s_code_is_an_enum_and_still_a_string() -> None:
    known = HostError("not_granted", "chat.post is not granted")
    unknown = HostError("something_new", "a code this SDK predates")

    assert known.code == "not_granted"
    assert known.code is HostErrorCode.NOT_GRANTED
    assert unknown.code == "something_new"
