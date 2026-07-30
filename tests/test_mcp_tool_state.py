"""Tests for ``MCPToolStateStore`` — file-backed disabled-tools state.

Pins the store's contract in isolation from ``MCPClientManager``:
* No project_dir → no-op (load returns empty, save silently drops).
* Round-trip through disk preserves the ``{server: set[tool_name]}``
  shape.
* Malformed JSON on disk degrades to empty rather than raising.
* Empty inner sets are pruned by save so the file stays clean.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.mcp.tool_state import MCPToolStateStore


class TestPath:
    def test_none_project_dir_returns_none(self):
        assert MCPToolStateStore(None).path() is None

    def test_resolves_relative_to_ember_subdir(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert store.path() == tmp_path / ".ember" / "mcp-tool-state.json"


class TestLoad:
    def test_missing_returns_empty(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}

    def test_no_project_dir_returns_empty(self):
        assert MCPToolStateStore(None).load() == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        (tmp_path / ".ember").mkdir()
        (tmp_path / ".ember" / "mcp-tool-state.json").write_text("{not json")
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}

    def test_valid_state_returns_sets(self, tmp_path: Path):
        (tmp_path / ".ember").mkdir()
        (tmp_path / ".ember" / "mcp-tool-state.json").write_text(
            '{"disabled": {"srv1": ["tool_a", "tool_b"], "srv2": ["tool_c"]}}'
        )
        store = MCPToolStateStore(tmp_path)
        state = store.load()
        assert state == {"srv1": {"tool_a", "tool_b"}, "srv2": {"tool_c"}}

    def test_missing_disabled_key_returns_empty(self, tmp_path: Path):
        (tmp_path / ".ember").mkdir()
        (tmp_path / ".ember" / "mcp-tool-state.json").write_text('{"other": "data"}')
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}


class TestSave:
    def test_no_project_dir_is_noop(self):
        MCPToolStateStore(None).save({"srv": {"tool"}})  # should not raise

    def test_writes_file(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        store.save({"srv": {"tool_a", "tool_b"}})
        p = tmp_path / ".ember" / "mcp-tool-state.json"
        assert p.exists()

    def test_round_trip_preserves_state(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        original = {"srv1": {"tool_a", "tool_b"}, "srv2": {"tool_c"}}
        store.save(original)
        assert store.load() == original

    def test_empty_inner_sets_pruned(self, tmp_path: Path):
        # A server with no disabled tools should not appear in the
        # saved blob — keeps the file clean when a tool gets
        # re-enabled and the caller doesn't pop the empty set.
        import json

        store = MCPToolStateStore(tmp_path)
        store.save({"srv1": {"tool"}, "srv2": set()})
        payload = json.loads((tmp_path / ".ember" / "mcp-tool-state.json").read_text())
        assert payload["disabled"] == {"srv1": ["tool"]}

    def test_creates_ember_subdir(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert not (tmp_path / ".ember").exists()
        store.save({"srv": {"tool"}})
        assert (tmp_path / ".ember").is_dir()

    def test_overwrites_existing(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        store.save({"srv": {"tool_a"}})
        store.save({"srv": {"tool_b"}})
        assert store.load() == {"srv": {"tool_b"}}
