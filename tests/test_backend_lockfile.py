"""Unit coverage for the per-project backend discovery lockfile."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from ember_code.backend.lockfile import (
    Lockfile,
    discover,
    is_pid_alive,
    is_port_reachable,
)


class TestLockfile:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.write(pid=1234, port=55555, wire_version="0.9.1")

        data = lock.read()
        assert data is not None
        assert data["pid"] == 1234
        assert data["port"] == 55555
        assert data["wire_version"] == "0.9.1"
        assert data["created_at"] > 0

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert Lockfile(tmp_path).read() is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text("this is not json{", encoding="utf-8")
        assert lock.read() is None

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        # No file yet — no error.
        lock.remove()
        lock.write(pid=1, port=1, wire_version="x")
        assert lock.path.exists()
        lock.remove()
        assert not lock.path.exists()
        # And again — still no error.
        lock.remove()

    def test_write_is_atomic(self, tmp_path: Path) -> None:
        """After a write, no ``.tmp`` sibling should be left behind —
        ``os.replace`` renames the tempfile onto the target."""
        lock = Lockfile(tmp_path)
        lock.write(pid=1, port=1, wire_version="x")
        siblings = list(lock.path.parent.iterdir())
        assert len(siblings) == 1
        assert siblings[0] == lock.path

    def test_path_uses_resolved_project_dir(self, tmp_path: Path) -> None:
        """``resolve()`` in the constructor means two paths pointing
        at the same directory (e.g. via symlink or ``/tmp`` vs
        ``/private/tmp`` on macOS) share the same lockfile."""
        real = tmp_path / "real"
        real.mkdir()
        symlink = tmp_path / "via-link"
        symlink.symlink_to(real, target_is_directory=True)

        lock_a = Lockfile(real)
        lock_b = Lockfile(symlink)
        assert lock_a.path.resolve() == lock_b.path.resolve()


class TestIsPidAlive:
    def test_current_process_alive(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_impossible_pid_is_dead(self) -> None:
        # PID 0 and negative are never real processes.
        assert is_pid_alive(0) is False
        assert is_pid_alive(-1) is False

    def test_far_future_pid_is_dead(self) -> None:
        # ``pid_max`` is typically 4M on Linux, 99998 on macOS.
        # Anything above 10M is guaranteed to be unassigned.
        assert is_pid_alive(2**31 - 1) is False


class TestIsPortReachable:
    def test_unlikely_port_unreachable(self) -> None:
        # ``0`` isn't a valid client target; the OS should refuse
        # the connection immediately.
        assert is_port_reachable(0, timeout=0.1) is False

    def test_live_port_reachable(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert is_port_reachable(port, timeout=0.5) is True
        finally:
            srv.close()


class TestDiscover:
    def test_returns_none_when_no_lockfile(self, tmp_path: Path) -> None:
        assert discover(tmp_path, expected_wire_version="0.9.1") is None

    def test_stale_dead_pid_lock_is_removed(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        # Impossible PID + fake port. ``discover`` should treat as
        # stale, remove the lockfile, and return ``None``.
        lock.write(pid=2**31 - 1, port=1, wire_version="0.9.1")
        assert lock.path.exists()

        result = discover(tmp_path, expected_wire_version="0.9.1")
        assert result is None
        assert not lock.path.exists()

    def test_alive_pid_but_dead_port_lock_is_removed(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        # ``os.getpid()`` is us — definitely alive — but we bind no
        # port. Discovery should treat as stale.
        lock.write(pid=os.getpid(), port=1, wire_version="0.9.1")

        result = discover(tmp_path, expected_wire_version="0.9.1")
        assert result is None
        assert not lock.path.exists()

    def test_version_mismatch_keeps_lockfile_and_signals(self, tmp_path: Path) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            lock = Lockfile(tmp_path)
            lock.write(pid=os.getpid(), port=port, wire_version="0.8.0")

            result = discover(tmp_path, expected_wire_version="0.9.1")
            assert result is not None
            assert result["_version_mismatch"] is True
            assert result["wire_version"] == "0.8.0"
            # Lockfile intact — the running BE is legitimately
            # owned; only the caller-facing notification changes.
            assert lock.path.exists()
        finally:
            srv.close()

    def test_healthy_lock_returns_payload(self, tmp_path: Path) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            lock = Lockfile(tmp_path)
            lock.write(pid=os.getpid(), port=port, wire_version="0.9.1")

            result = discover(tmp_path, expected_wire_version="0.9.1")
            assert result is not None
            assert "_version_mismatch" not in result
            assert result["pid"] == os.getpid()
            assert result["port"] == port
            assert result["wire_version"] == "0.9.1"
        finally:
            srv.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
