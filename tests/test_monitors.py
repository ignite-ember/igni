"""Tests for the plugin monitor primitive (row 33) — config
parsing, manager lifecycle, agent tools.

Lifecycle tests spawn real subprocesses via ``sh -c`` so the
manager exercises actual stdout drain + termination paths. We
stick to fast/short commands (sleep, true, echo) so the suite
doesn't slow down meaningfully.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ember_code.core.monitors.config import (
    MonitorConfig,
    _parse_monitors_dict,
    load_monitor_config,
)
from ember_code.core.monitors.manager import MonitorManager
from ember_code.core.tools.monitors import MonitorTools

# ── Config parsing ────────────────────────────────────────────


class TestParseMonitorsDict:
    def test_minimal(self):
        out = _parse_monitors_dict({"monitors": {"x": {"command": "cmd"}}})
        assert "x" in out
        assert out["x"].command == "cmd"
        # Defaults applied.
        assert out["x"].args == []
        assert out["x"].env == {}
        assert out["x"].restart == "on_crash"
        assert out["x"].cwd is None

    def test_full(self):
        out = _parse_monitors_dict(
            {
                "monitors": {
                    "watcher": {
                        "command": "npm",
                        "args": ["run", "watch"],
                        "cwd": "frontend",
                        "env": {"NODE_ENV": "development"},
                        "restart": "always",
                    }
                }
            }
        )
        cfg = out["watcher"]
        assert cfg.args == ["run", "watch"]
        assert cfg.cwd == "frontend"
        assert cfg.env == {"NODE_ENV": "development"}
        assert cfg.restart == "always"

    def test_unknown_restart_policy_defaults(self):
        """``restart: "maybe"`` falls back to ``on_crash`` — the
        permissive parser does not bounce the row."""
        out = _parse_monitors_dict({"monitors": {"x": {"command": "cmd", "restart": "maybe"}}})
        assert out["x"].restart == "on_crash"

    def test_namespace_prefix(self):
        out = _parse_monitors_dict(
            {"monitors": {"watcher": {"command": "x"}}},
            namespace="my-plugin",
        )
        assert "my-plugin:watcher" in out
        assert "watcher" not in out

    def test_skips_malformed_rows(self):
        out = _parse_monitors_dict(
            {
                "monitors": {
                    "good": {"command": "ok"},
                    "no_cmd": {"args": ["--stdio"]},
                    "bad_type": "command",
                }
            }
        )
        assert set(out.keys()) == {"good"}

    def test_no_monitors_key(self):
        assert _parse_monitors_dict({}) == {}
        assert _parse_monitors_dict({"monitors": "not a dict"}) == {}


class TestLoadMonitorConfig:
    def _write(self, p: Path, payload: dict) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))

    def test_project_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        self._write(
            tmp_path / ".monitors.json",
            {"monitors": {"x": {"command": "cmd"}}},
        )
        out = load_monitor_config(tmp_path)
        assert "x" in out

    def test_project_overrides_user(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        self._write(
            home / ".ember" / "monitors.json",
            {"monitors": {"shared": {"command": "user-cmd"}}},
        )
        self._write(
            tmp_path / ".monitors.json",
            {"monitors": {"shared": {"command": "project-cmd"}}},
        )
        out = load_monitor_config(tmp_path)
        assert out["shared"].command == "project-cmd"

    def test_plugin_namespacing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        plugin_dir = tmp_path / "plugins" / "my-plugin"
        plugin_dir.mkdir(parents=True)
        self._write(
            plugin_dir / ".monitors.json",
            {"monitors": {"watcher": {"command": "x"}}},
        )
        out = load_monitor_config(
            tmp_path,
            plugin_roots=[(plugin_dir, "my-plugin")],
        )
        assert "my-plugin:watcher" in out

    def test_invalid_json_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        (tmp_path / ".monitors.json").write_text("{not json")
        assert load_monitor_config(tmp_path) == {}


# ── Manager lifecycle ────────────────────────────────────────


# Use Python (always available) for portable subprocess test
# fixtures — ``sh -c`` works on darwin/linux but not Windows CI.
PY = sys.executable


class TestMonitorManager:
    @pytest.mark.asyncio
    async def test_start_and_stop_short_lived(self, tmp_path):
        """``true``-like one-shot exits cleanly; monitor goes to
        ``stopped`` (not ``failed``) because the policy was
        ``never``."""
        cfg = MonitorConfig(
            name="oneshot",
            command=PY,
            args=["-c", "print('hi')"],
            restart="never",
        )
        manager = MonitorManager({"oneshot": cfg}, tmp_path)
        await manager.start_all()
        # Wait for the supervisor to observe the exit.
        for _ in range(50):
            if manager._handles["oneshot"].status != "running":
                break
            await asyncio.sleep(0.05)
        snap = manager.snapshot_all()
        assert snap[0]["status"] == "stopped"
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_drain_captures_stdout(self, tmp_path):
        cfg = MonitorConfig(
            name="echo",
            command=PY,
            args=["-c", "import sys; print('first'); print('second'); sys.stdout.flush()"],
            restart="never",
        )
        manager = MonitorManager({"echo": cfg}, tmp_path)
        await manager.start_all()
        # Give the drain task a tick to read stdout before the
        # supervisor tears down.
        for _ in range(50):
            tail = manager.output_tail("echo")
            if "second" in tail:
                break
            await asyncio.sleep(0.05)
        tail = manager.output_tail("echo")
        assert "first" in tail
        assert "second" in tail
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_stop_terminates_running_monitor(self, tmp_path):
        """``stop`` SIGTERMs a long-running monitor and the
        snapshot transitions to ``stopped``."""
        cfg = MonitorConfig(
            name="sleeper",
            command=PY,
            args=["-c", "import time; time.sleep(60)"],
            restart="never",
        )
        manager = MonitorManager({"sleeper": cfg}, tmp_path)
        await manager.start_all()
        # Wait until launched.
        for _ in range(50):
            if manager._handles["sleeper"].status == "running":
                break
            await asyncio.sleep(0.05)
        assert manager._handles["sleeper"].status == "running"
        await manager.stop("sleeper")
        assert manager._handles["sleeper"].status == "stopped"

    @pytest.mark.asyncio
    async def test_failed_launch_marks_status_failed(self, tmp_path):
        """An unexecutable command marks the monitor ``failed``
        and surfaces the error in the output buffer — the
        supervisor doesn't get a process to wait on."""
        cfg = MonitorConfig(
            name="ghost",
            command="/this/binary/does/not/exist_xyz",
            restart="never",
        )
        manager = MonitorManager({"ghost": cfg}, tmp_path)
        await manager.start_all()
        handle = manager._handles["ghost"]
        assert handle.status == "failed"
        joined = "\n".join(handle.output_tail(40))
        assert "failed to launch" in joined
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_restart_clears_crash_counter(self, tmp_path):
        """``restart`` is the explicit-user path: it forgets the
        crash count and re-launches even a ``failed`` monitor."""
        cfg = MonitorConfig(
            name="r",
            command=PY,
            args=["-c", "print('once')"],
            restart="never",
        )
        manager = MonitorManager({"r": cfg}, tmp_path)
        await manager.start_all()
        # Let it run + exit + supervisor settle.
        for _ in range(50):
            if manager._handles["r"].status == "stopped":
                break
            await asyncio.sleep(0.05)
        # Inflate the crash counter to confirm it gets cleared.
        manager._handles["r"]._crash_count = 99
        manager._handles["r"]._status = "failed"
        await manager.restart("r")
        assert manager._handles["r"]._crash_count == 0
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_snapshot_for_never_started_monitor(self, tmp_path):
        """A configured-but-unstarted monitor shows up as
        ``stopped`` so the panel doesn't omit it. The manager
        constructs ``MonitorHandle`` lazily on first start."""
        cfg = MonitorConfig(name="x", command="echo")
        manager = MonitorManager({"x": cfg}, tmp_path)
        snap = manager.snapshot_all()
        assert len(snap) == 1
        assert snap[0]["name"] == "x"
        assert snap[0]["status"] == "stopped"
        assert snap[0]["pid"] is None

    @pytest.mark.asyncio
    async def test_shutdown_all_idempotent(self, tmp_path):
        cfg = MonitorConfig(
            name="s",
            command=PY,
            args=["-c", "import time; time.sleep(60)"],
            restart="never",
        )
        manager = MonitorManager({"s": cfg}, tmp_path)
        await manager.start_all()
        await manager.shutdown_all()
        # Second call must be safe — no double-cleanup explosions.
        await manager.shutdown_all()


# ── Agent tools ──────────────────────────────────────────────


class TestMonitorTools:
    def test_status_returns_json(self, tmp_path):
        cfg = MonitorConfig(name="x", command="echo")
        manager = MonitorManager({"x": cfg}, tmp_path)
        tool = MonitorTools(manager)
        out = tool.monitor_status()
        parsed = json.loads(out)
        assert parsed[0]["name"] == "x"

    def test_status_empty(self, tmp_path):
        manager = MonitorManager({}, tmp_path)
        tool = MonitorTools(manager)
        assert "No monitors" in tool.monitor_status()

    def test_output_unknown_monitor(self, tmp_path):
        manager = MonitorManager({}, tmp_path)
        tool = MonitorTools(manager)
        assert "Error" in tool.monitor_output("nope", lines=5)

    def test_output_returns_tail(self, tmp_path):
        cfg = MonitorConfig(name="x", command="echo")
        manager = MonitorManager({"x": cfg}, tmp_path)
        # Pre-populate the buffer to avoid spawning a real
        # process in this pure-tool test.
        handle = manager._handles
        from ember_code.core.monitors.manager import MonitorHandle

        h = MonitorHandle(cfg, project_dir=tmp_path)
        h._output.append("line 1")
        h._output.append("line 2")
        handle["x"] = h
        tool = MonitorTools(manager)
        out = tool.monitor_output("x", lines=10)
        assert "line 1" in out
        assert "line 2" in out

    def test_output_lines_int_coercion(self, tmp_path):
        """The model sometimes passes ``"5"`` as a string for
        integer-typed params — we coerce defensively."""
        cfg = MonitorConfig(name="x", command="echo")
        manager = MonitorManager({"x": cfg}, tmp_path)
        from ember_code.core.monitors.manager import MonitorHandle

        h = MonitorHandle(cfg, project_dir=tmp_path)
        h._output.append("hi")
        manager._handles["x"] = h
        tool = MonitorTools(manager)
        out = tool.monitor_output("x", lines="3")  # type: ignore[arg-type]
        assert "hi" in out

    def test_output_lines_invalid(self, tmp_path):
        cfg = MonitorConfig(name="x", command="echo")
        manager = MonitorManager({"x": cfg}, tmp_path)
        tool = MonitorTools(manager)
        out = tool.monitor_output("x", lines="abc")  # type: ignore[arg-type]
        assert "Error" in out and "integer" in out

    @pytest.mark.asyncio
    async def test_restart_calls_manager(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        manager = MagicMock()
        manager.restart = AsyncMock(return_value="Restarted x.")
        tool = MonitorTools(manager)
        out = await tool.monitor_restart("x")
        assert "Restarted" in out
        manager.restart.assert_called_with("x")

    @pytest.mark.asyncio
    async def test_stop_calls_manager(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        manager = MagicMock()
        manager.stop = AsyncMock(return_value="Stopped x.")
        tool = MonitorTools(manager)
        out = await tool.monitor_stop("x")
        assert "Stopped" in out

    @pytest.mark.asyncio
    async def test_restart_surfaces_manager_exception(self):
        from unittest.mock import AsyncMock, MagicMock

        manager = MagicMock()
        manager.restart = AsyncMock(side_effect=RuntimeError("boom"))
        tool = MonitorTools(manager)
        out = await tool.monitor_restart("x")
        assert "Error" in out
        assert "boom" in out
