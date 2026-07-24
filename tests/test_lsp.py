"""Tests for the LSP primitive — config parsing, client framing,
manager lifecycle, and the ``lsp_query`` agent tool.

The LSP client uses real subprocess + stdio framing, so for the
client tests we either:
- Construct an ``LspClient`` and feed bytes directly to
  ``_read_message`` (framing-only tests, no subprocess).
- Spawn a tiny in-tree Python "echo" LSP server that responds to
  initialize / shutdown and an ``echo`` request (end-to-end test).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.lsp import manager as manager_mod
from ember_code.core.lsp.client import LspClient, LspClientError
from ember_code.core.lsp.loader import LspConfigLoader, load_lsp_config
from ember_code.core.lsp.manager import LspServerManager
from ember_code.core.lsp.schemas import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    LspConfigFile,
    LspServerConfig,
)
from ember_code.core.tools.lsp import LspTools

# ── Config parsing ────────────────────────────────────────────


def _parse_servers_dict(raw: dict, namespace: str = "") -> dict[str, LspServerConfig]:
    """Test helper — replicates the pre-refactor free function
    on top of :meth:`LspServerConfig.from_raw` and
    :class:`LspConfigFile` so the semantic contract stays under
    test without re-introducing a module-level parser."""
    try:
        typed = LspConfigFile.model_validate(raw if isinstance(raw, dict) else {})
    except Exception:
        return {}
    out: dict[str, LspServerConfig] = {}
    for name, entry in typed.lspServers.items():
        parsed = LspServerConfig.from_raw(name, entry, namespace=namespace)
        if parsed is not None:
            out[parsed.name] = parsed
    return out


class TestParseServersDict:
    def test_valid_minimal_entry(self):
        out = _parse_servers_dict({"lspServers": {"pyright": {"command": "pyright-langserver"}}})
        assert "pyright" in out
        assert out["pyright"].command == "pyright-langserver"
        # Defaults populated.
        assert out["pyright"].args == []
        assert out["pyright"].languages == []
        assert out["pyright"].root_uri is None
        assert out["pyright"].initialization_options == {}

    def test_full_entry(self):
        out = _parse_servers_dict(
            {
                "lspServers": {
                    "tsserver": {
                        "command": "typescript-language-server",
                        "args": ["--stdio"],
                        "languages": ["typescript", "javascript"],
                        "rootUri": "file:///specific/dir",
                        "initializationOptions": {"some": "opt"},
                        "env": {"FOO": "bar"},
                    }
                }
            }
        )
        cfg = out["tsserver"]
        assert cfg.args == ["--stdio"]
        assert cfg.languages == ["typescript", "javascript"]
        assert cfg.root_uri == "file:///specific/dir"
        assert cfg.initialization_options == {"some": "opt"}
        assert cfg.env == {"FOO": "bar"}

    def test_namespace_prefix(self):
        """Plugin-tier configs get a ``<plugin>:`` prefix so the
        same simple name (``pyright``) can come from multiple
        plugins without collision."""
        out = _parse_servers_dict(
            {"lspServers": {"pyright": {"command": "pyright"}}},
            namespace="mypy-tools",
        )
        assert "mypy-tools:pyright" in out
        assert "pyright" not in out

    def test_skips_invalid_entries(self):
        """Missing command, non-string command, non-dict entry —
        all drop the row instead of sinking the file."""
        out = _parse_servers_dict(
            {
                "lspServers": {
                    "good": {"command": "good-ls"},
                    "no_cmd": {"args": ["--stdio"]},
                    "bad_cmd_type": {"command": 42},
                    "not_a_dict": "command",
                }
            }
        )
        assert set(out.keys()) == {"good"}

    def test_accepts_snake_case_aliases(self):
        """Allow ``root_uri`` / ``initialization_options`` as
        Python-natural aliases for CC's camelCase keys."""
        out = _parse_servers_dict(
            {
                "lspServers": {
                    "x": {
                        "command": "x",
                        "root_uri": "file:///x",
                        "initialization_options": {"snake": True},
                    }
                }
            }
        )
        assert out["x"].root_uri == "file:///x"
        assert out["x"].initialization_options == {"snake": True}

    def test_no_lsp_servers_key_returns_empty(self):
        assert _parse_servers_dict({}) == {}
        assert _parse_servers_dict({"lspServers": "not a dict"}) == {}

    def test_camelcase_alias_wins_on_collision(self):
        """When both ``rootUri`` (alias) and ``root_uri`` (snake)
        appear in one entry, the LSP-spec camelCase key wins.
        Documenting the tie-break so future field additions don't
        accidentally flip it."""
        out = _parse_servers_dict(
            {
                "lspServers": {
                    "x": {
                        "command": "x",
                        "rootUri": "file:///camel",
                        "root_uri": "file:///snake",
                    }
                }
            }
        )
        assert out["x"].root_uri == "file:///camel"


class TestLoadLspConfig:
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def test_project_only(self, tmp_path, monkeypatch):
        """Uses the back-compat ``load_lsp_config`` wrapper — the
        legacy return type is a bare ``dict[str, LspServerConfig]``."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        self._write_json(
            tmp_path / ".lsp.json",
            {"lspServers": {"pyright": {"command": "pyright"}}},
        )
        out = load_lsp_config(tmp_path)
        assert "pyright" in out

    def test_user_overridden_by_project(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        self._write_json(
            home / ".ember" / "lsp.json",
            {"lspServers": {"shared": {"command": "user-cmd"}}},
        )
        self._write_json(
            tmp_path / ".lsp.json",
            {"lspServers": {"shared": {"command": "project-cmd"}}},
        )
        out = load_lsp_config(tmp_path)
        # Project wins (loaded after user).
        assert out["shared"].command == "project-cmd"

    def test_plugin_namespacing(self, tmp_path, monkeypatch):
        """Uses :class:`LspConfigLoader` directly to exercise the
        typed result surface (and its typed error list)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        plugin_dir = tmp_path / "plugins" / "my-plugin"
        plugin_dir.mkdir(parents=True)
        self._write_json(
            plugin_dir / ".lsp.json",
            {"lspServers": {"pyright": {"command": "plugin-pyright"}}},
        )
        result = LspConfigLoader(
            tmp_path,
            plugin_roots=[(plugin_dir, "my-plugin")],
        ).load()
        assert "my-plugin:pyright" in result.servers
        assert result.servers["my-plugin:pyright"].command == "plugin-pyright"
        assert result.errors == []

    def test_missing_files_return_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        assert load_lsp_config(tmp_path) == {}

    def test_invalid_json_is_no_op(self, tmp_path, monkeypatch):
        """A malformed ``.lsp.json`` shouldn't sink config loading
        — the back-compat wrapper still yields an empty dict."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        (tmp_path / ".lsp.json").write_text("{not json")
        out = load_lsp_config(tmp_path)
        assert out == {}

    def test_invalid_json_surfaces_as_typed_error(self, tmp_path, monkeypatch):
        """The typed :class:`LspConfigLoadResult` surfaces the
        parse failure so the panel can show *why* a file was
        skipped."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        (tmp_path / ".lsp.json").write_text("{not json")
        result = LspConfigLoader(tmp_path).load()
        assert result.servers == {}
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.entry_name is None
        assert err.path.endswith(".lsp.json")
        assert "read/decode failed" in err.reason

    def test_missing_command_surfaces_as_per_entry_error(self, tmp_path, monkeypatch):
        """A per-entry parse failure lands in ``.errors`` with the
        offending server key set."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        self._write_json(
            tmp_path / ".lsp.json",
            {"lspServers": {"bad": {"args": ["--stdio"]}}},
        )
        result = LspConfigLoader(tmp_path).load()
        assert result.servers == {}
        assert any(e.entry_name == "bad" and "command" in e.reason for e in result.errors)


# ── Client framing (no subprocess) ────────────────────────────


def _frame(body: bytes) -> bytes:
    """Helper — wrap a body in LSP framing."""
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def _stream(data: bytes) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with bytes for tests."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestClientFraming:
    @pytest.mark.asyncio
    async def test_reads_one_message(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"hello": 1}}).encode()
        msg = await client._read_message(_stream(_frame(body)))
        assert isinstance(msg, JsonRpcResponse)
        assert msg.id == 1
        assert msg.result == {"hello": 1}

    @pytest.mark.asyncio
    async def test_eof_returns_none(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        msg = await client._read_message(_stream(b""))
        assert msg is None

    @pytest.mark.asyncio
    async def test_missing_content_length_returns_none(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        # Headers without Content-Length — invalid framing.
        msg = await client._read_message(_stream(b"X-Other: 1\r\n\r\n"))
        assert msg is None

    @pytest.mark.asyncio
    async def test_invalid_utf8_body_returns_none(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        msg = await client._read_message(_stream(_frame(b"\xff\xfe")))
        assert msg is None


class TestClientDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_resolves_pending_future(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        client._pending[7] = fut
        client._dispatch(JsonRpcResponse(id=7, result={"ok": True}))
        assert (await fut) == {"ok": True}

    @pytest.mark.asyncio
    async def test_dispatch_surfaces_error_response(self):
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        client._pending[7] = fut
        client._dispatch(
            JsonRpcResponse.model_validate(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        )
        with pytest.raises(LspClientError) as exc_info:
            await fut
        assert "Method not found" in str(exc_info.value)
        assert "-32601" in str(exc_info.value)

    def test_dispatch_ignores_unmatched_response(self):
        """Server might send a response for a request we already
        timed out on. Don't crash."""
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        # No pending future for id=99. Should not raise.
        client._dispatch(JsonRpcResponse(id=99, result=None))

    def test_dispatch_ignores_notifications(self):
        """A no-id message is a server-side notification
        (``window/logMessage``, etc.). We drop them silently."""
        client = LspClient(
            LspServerConfig(name="x", command="x"),
            project_dir=Path("/"),
        )
        client._dispatch(JsonRpcNotification(method="window/logMessage", params={}))

    def test_shutdown_request_serializes_params_as_null(self):
        """The LSP ``shutdown`` request and ``exit`` notification
        pass ``params=None`` — some servers require the key to be
        present as JSON ``null``, not omitted. Guard the wire
        shape so a future ``exclude_none=True`` regression is
        caught here."""
        envelope = JsonRpcRequest(id=1, method="shutdown", params=None)
        payload = envelope.model_dump(exclude_none=False)
        assert payload == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "shutdown",
            "params": None,
        }
        assert (
            json.dumps(payload)
            == '{"jsonrpc": "2.0", "id": 1, "method": "shutdown", "params": null}'
        )


# ── Manager lifecycle ────────────────────────────────────────


class TestLspServerManager:
    def test_list_servers_empty(self, tmp_path):
        manager = LspServerManager({}, tmp_path)
        assert manager.list_servers() == []

    def test_list_servers_sorted(self, tmp_path):
        configs = {
            "z": LspServerConfig(name="z", command="z"),
            "a": LspServerConfig(name="a", command="a"),
            "m": LspServerConfig(name="m", command="m"),
        }
        manager = LspServerManager(configs, tmp_path)
        assert manager.list_servers() == ["a", "m", "z"]

    @pytest.mark.asyncio
    async def test_ensure_unknown_server_raises(self, tmp_path):
        manager = LspServerManager({}, tmp_path)
        with pytest.raises(LspClientError) as exc_info:
            await manager.ensure("nope")
        assert "not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ensure_caches_started_client(self, tmp_path, monkeypatch):
        """Two ensure() calls return the same client — we never
        re-launch a running server."""
        configs = {"x": LspServerConfig(name="x", command="x")}
        manager = LspServerManager(configs, tmp_path)

        start_count = 0

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def start(self):
                nonlocal start_count
                start_count += 1

            async def shutdown(self):
                pass

            async def request(self, method, params):
                return {"echoed": method}

        monkeypatch.setattr(manager_mod, "LspClient", _FakeClient)
        await manager.ensure("x")
        await manager.ensure("x")
        assert start_count == 1

    @pytest.mark.asyncio
    async def test_ensure_records_launch_error(self, tmp_path, monkeypatch):
        """Launch failure is recorded for ``last_error`` so the
        panel can surface the reason without re-launching."""
        configs = {"x": LspServerConfig(name="x", command="not-a-real-binary-xyz")}
        manager = LspServerManager(configs, tmp_path)

        class _BrokenClient:
            def __init__(self, *a, **kw):
                pass

            async def start(self):
                raise LspClientError("launch failed: no such binary")

            async def shutdown(self):
                pass

        monkeypatch.setattr(manager_mod, "LspClient", _BrokenClient)
        with pytest.raises(LspClientError):
            await manager.ensure("x")
        assert "launch failed" in manager.last_error("x")

    @pytest.mark.asyncio
    async def test_query_routes_through_ensure(self, tmp_path, monkeypatch):
        configs = {"x": LspServerConfig(name="x", command="x")}
        manager = LspServerManager(configs, tmp_path)

        captured = []

        class _Recording:
            def __init__(self, *a, **kw):
                pass

            async def start(self):
                pass

            async def shutdown(self):
                pass

            async def request(self, method, params):
                captured.append((method, params))
                return {"result_for": method}

        monkeypatch.setattr(manager_mod, "LspClient", _Recording)
        out = await manager.query("x", "textDocument/hover", {"foo": 1})
        assert out == {"result_for": "textDocument/hover"}
        assert captured == [("textDocument/hover", {"foo": 1})]

    @pytest.mark.asyncio
    async def test_shutdown_all_clears_clients(self, tmp_path, monkeypatch):
        configs = {"x": LspServerConfig(name="x", command="x")}
        manager = LspServerManager(configs, tmp_path)

        shutdown_calls = []

        class _Tracked:
            def __init__(self, *a, **kw):
                pass

            async def start(self):
                pass

            async def shutdown(self):
                shutdown_calls.append(1)

            async def request(self, method, params):
                return None

        monkeypatch.setattr(manager_mod, "LspClient", _Tracked)
        await manager.ensure("x")
        assert manager.is_running("x")
        await manager.shutdown_all()
        assert shutdown_calls == [1]
        assert not manager.is_running("x")


# ── Agent tool (lsp_query) ────────────────────────────────────


class TestLspTools:
    @pytest.mark.asyncio
    async def test_query_returns_result_as_json(self):
        manager = MagicMock()
        manager.query = AsyncMock(return_value={"line": 1, "character": 2})
        tool = LspTools(manager)
        result = await tool.lsp_query("pyright", "textDocument/definition", "{}")
        parsed = json.loads(result)
        assert parsed == {"line": 1, "character": 2}

    @pytest.mark.asyncio
    async def test_null_result_returned_as_null_string(self):
        """LSP commonly returns null for "no info" — surface it
        explicitly so the agent doesn't read it as an empty
        success."""
        manager = MagicMock()
        manager.query = AsyncMock(return_value=None)
        tool = LspTools(manager)
        out = await tool.lsp_query("x", "textDocument/hover", "")
        assert out == "null"

    @pytest.mark.asyncio
    async def test_empty_params_treated_as_null(self):
        manager = MagicMock()
        manager.query = AsyncMock(return_value="ok")
        tool = LspTools(manager)
        await tool.lsp_query("x", "shutdown", "")
        manager.query.assert_called_with("x", "shutdown", None)

    @pytest.mark.asyncio
    async def test_invalid_json_params_returns_error(self):
        manager = MagicMock()
        tool = LspTools(manager)
        out = await tool.lsp_query("x", "method", "{not json")
        assert "Error" in out
        assert "valid JSON" in out

    @pytest.mark.asyncio
    async def test_lsp_client_error_surfaced(self):
        manager = MagicMock()
        manager.query = AsyncMock(side_effect=LspClientError("server gone"))
        tool = LspTools(manager)
        out = await tool.lsp_query("x", "method", "{}")
        assert "Error" in out
        assert "server gone" in out

    @pytest.mark.asyncio
    async def test_list_servers_returns_json(self):
        manager = LspServerManager(
            {"pyright": LspServerConfig(name="pyright", command="pyright", languages=["python"])},
            Path("/tmp"),
        )
        tool = LspTools(manager)
        out = tool.lsp_list_servers()
        parsed = json.loads(out)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "pyright"
        assert parsed[0]["languages"] == ["python"]
        assert parsed[0]["running"] is False

    def test_list_servers_empty(self):
        manager = LspServerManager({}, Path("/tmp"))
        tool = LspTools(manager)
        out = tool.lsp_list_servers()
        assert "No LSP servers configured" in out
