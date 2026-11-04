"""The MCP tool filter copes with a toolkit it never saw connect.

:class:`MCPToolFilter` normally snapshots a server's functions at connect
(the real-server path is in
``test_an_mcp_server_is_connected_filtered_and_asked_for_more.py``). These
are the edges around that: a toolkit handed to it with no snapshot, a
toolkit whose ``functions`` is not a dict, and no toolkit at all. The
toolkit is a plain object with a ``functions`` dict — the one attribute
the filter reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ember_code.core.mcp.tool_filter import MCPToolFilter
from ember_code.core.mcp.tool_state import MCPToolStateStore


@dataclass
class _Function:
    description: str | None = None
    requires_confirmation: bool = False


@dataclass
class _Toolkit:
    functions: Any = field(default_factory=dict)


def _filter(tmp_path: Path) -> MCPToolFilter:
    return MCPToolFilter(MCPToolStateStore(tmp_path))


def _toolkit() -> _Toolkit:
    return _Toolkit({"search": _Function("Search the notes."), "erase": _Function(None)})


def test_a_toolkit_with_no_snapshot_is_snapshotted_on_first_filter(tmp_path):
    tool_filter = _filter(tmp_path)
    toolkit = _toolkit()
    tool_filter.set_enabled("notes", "erase", False, None)

    tool_filter.apply("notes", toolkit)

    assert list(toolkit.functions) == ["search"]
    # The lazily-taken snapshot still knows the hidden tool.
    assert tool_filter.list_tools("notes", toolkit) == ["search", "erase"]


def test_without_a_snapshot_the_live_toolkit_is_listed(tmp_path):
    tool_filter = _filter(tmp_path)
    toolkit = _toolkit()

    assert tool_filter.list_tools("notes", toolkit) == ["search", "erase"]
    assert tool_filter.tool_descriptions("notes", toolkit) == {
        "search": "Search the notes.",
        "erase": "",
    }
    assert [row.name for row in tool_filter.list_tool_info("notes", toolkit)] == [
        "search",
        "erase",
    ]


def test_with_no_toolkit_there_is_nothing_to_list(tmp_path):
    tool_filter = _filter(tmp_path)

    assert tool_filter.list_tools("notes", None) == []
    assert tool_filter.tool_descriptions("notes", None) == {}
    assert tool_filter.list_tool_info("notes", None) == []
    tool_filter.apply("notes", None)  # nothing to filter, nothing raised


def test_a_toolkit_whose_functions_is_not_a_dict_is_left_alone(tmp_path):
    tool_filter = _filter(tmp_path)
    odd = _Toolkit(functions=None)

    tool_filter.snapshot("notes", odd)
    tool_filter.apply("notes", odd)
    tool_filter.require_confirmation("notes", {"search"}, odd)

    assert odd.functions is None
    assert tool_filter.list_tools("notes", odd) == []


def test_a_confirmation_reaches_a_tool_known_only_from_the_snapshot(tmp_path):
    tool_filter = _filter(tmp_path)
    toolkit = _toolkit()
    tool_filter.snapshot("notes", toolkit)
    tool_filter.set_enabled("notes", "erase", False, toolkit)

    tool_filter.require_confirmation("notes", {"erase"}, None)
    tool_filter.set_enabled("notes", "erase", True, toolkit)

    assert toolkit.functions["erase"].requires_confirmation is True
    assert toolkit.functions["search"].requires_confirmation is False


def test_forgetting_a_server_drops_its_snapshot(tmp_path):
    tool_filter = _filter(tmp_path)
    tool_filter.snapshot("notes", _toolkit())

    tool_filter.forget("notes")

    assert tool_filter.list_tools("notes", _Toolkit({"fresh": _Function()})) == ["fresh"]
