"""Tools, signals and the environment, as an author meets them."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_extension import (
    IgniExtension,
    NotConfigured,
    Signal,
    ToolHints,
    ToolHintsMissing,
    WorkspaceMode,
)
from igni_extension.testing import HostHarness

READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


async def test_tools_are_registered_with_their_hints() -> None:
    ext = IgniExtension("acme.example", environ={})

    @ext.tool(hints=READS)
    async def greet(name: str) -> str:
        return f"hello {name}"

    @ext.tool(
        hints=ToolHints(read_only=False, destructive=True, idempotent=False, open_world=True),
        name="shout",
        description="Says it louder.",
    )
    async def loud(text: str) -> str:
        return text.upper()

    async with HostHarness(ext) as host:
        tools = {tool.name: tool for tool in (await host.session.list_tools()).tools}
        greeted = await host.session.call_tool("greet", {"name": "Ada"})
        shouted = await host.session.call_tool("shout", {"text": "hi"})

    assert set(tools) == {"greet", "shout"}
    assert tools["shout"].description == "Says it louder."
    assert greeted.content[0].text == "hello Ada"  # type: ignore[union-attr]
    assert shouted.content[0].text == "HI"  # type: ignore[union-attr]
    # What igni compares with the manifest: all four, as MCP spells them.
    assert tools["greet"].annotations is not None
    assert tools["greet"].annotations.readOnlyHint is True
    assert tools["greet"].annotations.openWorldHint is False
    assert tools["shout"].annotations is not None
    assert tools["shout"].annotations.destructiveHint is True


async def test_a_tool_that_leaves_a_hint_unstated_stops_the_extension_serving() -> None:
    """`ext.mcp` can be reached past `ext.tool`; the check is on what it serves."""
    ext = IgniExtension("acme.example", environ={})

    @ext.tool(hints=READS)
    async def fine() -> str:
        return "ok"

    async def unstated() -> str:
        return "?"

    ext.mcp.add_tool(unstated, name="unstated")

    with pytest.raises(ToolHintsMissing) as raised:
        await ext.check_tools()
    assert raised.value.tools == ["unstated"]


def test_a_read_cannot_also_be_destructive() -> None:
    with pytest.raises(ValidationError):
        ToolHints(read_only=True, destructive=True, idempotent=True, open_world=False)


async def test_a_signal_runs_its_handlers_with_the_value_it_carries() -> None:
    ext = IgniExtension("acme.example", environ={})
    changed: Signal[str] = Signal("sessions.current_changed", str)
    seen: list[str] = []

    @ext.on(changed)
    async def first(value: str) -> None:
        seen.append(f"first {value}")

    @ext.on(changed)
    async def broken(value: str) -> None:
        raise RuntimeError("one handler failing does not stop the next")

    @ext.on(changed)
    async def last(value: str) -> None:
        seen.append(f"last {value}")

    await ext.signals.emit(changed, "s-2")

    assert seen == ["first s-2", "last s-2"]


def test_the_environment_igni_grants_is_read_typed(tmp_path: Path) -> None:
    ext = IgniExtension(
        "acme.example",
        environ={
            "IGNI_WORKSPACE": str(tmp_path),
            "IGNI_WORKSPACE_MODE": "write",
            "IGNI_WORKSPACE_PATHS": "docs/**:src/*.py",
            "IGNI_EXTENSION_DATA": str(tmp_path / "data"),
            "IGNI_SECRET_LEDGER_TOKEN": "ledger",
            "IGNI_SERVICE_URL": "https://acme.example.test/",
        },
    )
    environment = ext.environment

    assert environment.workspace.granted and environment.workspace.writable
    assert environment.workspace.root == tmp_path
    assert environment.workspace.mode is WorkspaceMode.WRITE
    assert environment.workspace.paths == ("docs/**", "src/*.py")
    assert environment.data_dir == tmp_path / "data"
    assert environment.secrets.get("ledger-token") == "ledger"
    assert environment.service.url == "https://acme.example.test"


def test_what_it_ships_is_found_by_name(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    (assets / "neo4j" / "lib").mkdir(parents=True)
    (assets / "model").mkdir()
    (assets / "model" / "model.onnx").write_bytes(b"onnx")
    ext = IgniExtension("acme.example", environ={"IGNI_EXTENSION_ASSETS": str(assets)})

    assert ext.assets.path("neo4j") == assets / "neo4j"
    assert ext.assets.file("model", "model.onnx") == assets / "model" / "model.onnx"
    with pytest.raises(NotConfigured, match="no asset named 'jre'"):
        ext.assets.path("jre")
    with pytest.raises(NotConfigured, match="no file"):
        ext.assets.file("model", "tokenizer.json")


def test_outside_igni_there_are_no_assets() -> None:
    with pytest.raises(NotConfigured, match="laid out no assets"):
        IgniExtension("acme.example", environ={}).assets.path("neo4j")


def test_nothing_granted_is_nothing_granted() -> None:
    environment = IgniExtension("acme.example", environ={}).environment

    assert not environment.workspace.granted
    assert environment.data_dir is None
    assert environment.secrets.get("anything") is None
    assert not environment.service.configured
