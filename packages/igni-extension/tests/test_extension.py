"""Tools, signals and the environment, as an author meets them."""

from __future__ import annotations

from pathlib import Path

from igni_extension import IgniExtension, Signal, WorkspaceMode
from igni_extension.testing import HostHarness


async def test_tools_are_registered_with_or_without_arguments() -> None:
    ext = IgniExtension("acme.example", environ={})

    @ext.tool
    async def greet(name: str) -> str:
        return f"hello {name}"

    @ext.tool(name="shout", description="Says it louder.")
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
            "IGNI_CAPABILITY_GRAPH_DB_URI": "bolt://127.0.0.1:7687",
            "IGNI_CAPABILITY_GRAPH_DB_SECRET": "graph-password",
            "IGNI_CAPABILITY_EMBEDDER_URI": "http://127.0.0.1:9000",
            "IGNI_SECRET_GRAPH_PASSWORD": "s3cret",
            "IGNI_SECRET_LEDGER_TOKEN": "ledger",
            "IGNI_SERVICE_URL": "https://acme.example.test/",
        },
    )
    environment = ext.environment

    assert environment.workspace.granted and environment.workspace.writable
    assert environment.workspace.root == tmp_path
    assert environment.workspace.mode is WorkspaceMode.WRITE
    assert environment.workspace.paths == ("docs/**", "src/*.py")
    graph = environment.capabilities.get("graph-db")
    assert graph is not None
    assert (graph.uri, graph.secret_name, graph.secret) == (
        "bolt://127.0.0.1:7687",
        "graph-password",
        "s3cret",
    )
    assert "embedder" in environment.capabilities
    assert environment.secrets.get("ledger-token") == "ledger"
    assert environment.service.url == "https://acme.example.test"


def test_nothing_granted_is_nothing_granted() -> None:
    environment = IgniExtension("acme.example", environ={}).environment

    assert not environment.workspace.granted
    assert environment.capabilities.all() == []
    assert environment.secrets.get("anything") is None
    assert not environment.service.configured
