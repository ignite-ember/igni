"""Unit coverage for the per-project backend discovery lockfile."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from ember_code.backend.lockfile import (
    BackendDiscovery,
    Lockfile,
)
from ember_code.backend.schemas_lockfile import (
    DiscoveryOutcome,
    LiveBackend,
    LockfilePayload,
    NoBackend,
    RemoveLockfileResult,
    VersionMismatch,
    WriteLockfileResult,
)


class TestLockfile:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        payload = LockfilePayload.now(pid=1234, port=55555, wire_version="0.9.1")
        result = lock.write(payload)
        assert isinstance(result, WriteLockfileResult)
        assert result.ok is True
        assert result.payload is not None

        parsed = lock.read()
        assert parsed is not None
        assert isinstance(parsed, LockfilePayload)
        assert parsed.pid == 1234
        assert parsed.port == 55555
        assert parsed.wire_version == "0.9.1"
        assert parsed.created_at > 0

    def test_on_disk_wire_format_is_stable(self, tmp_path: Path) -> None:
        """The on-disk JSON MUST keep the exact key-set
        ``{pid, port, wire_version, created_at}`` — out-of-tree
        readers (VSCode extension, JB plugin, ``jq``) parse this
        file directly. Golden-file assertion guards the wire
        format."""
        lock = Lockfile(tmp_path)
        payload = LockfilePayload(
            pid=1234,
            port=55555,
            wire_version="0.9.1",
            created_at=1700000000,
        )
        result = lock.write(payload)
        assert result.ok is True

        raw = lock.path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert set(parsed.keys()) == {"pid", "port", "wire_version", "created_at"}
        assert parsed == {
            "pid": 1234,
            "port": 55555,
            "wire_version": "0.9.1",
            "created_at": 1700000000,
        }

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert Lockfile(tmp_path).read() is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text("this is not json{", encoding="utf-8")
        assert lock.read() is None

    def test_read_schema_mismatch_returns_none(self, tmp_path: Path) -> None:
        """A JSON dict missing required keys is treated as stale —
        overwritten on the next write, not raised loudly."""
        lock = Lockfile(tmp_path)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text('{"unrelated": "shape"}', encoding="utf-8")
        assert lock.read() is None

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        # No file yet — no error, existed=False.
        first = lock.remove()
        assert isinstance(first, RemoveLockfileResult)
        assert first.ok is True
        assert first.existed is False

        payload = LockfilePayload.now(pid=1, port=1, wire_version="x")
        lock.write(payload)
        assert lock.path.exists()

        second = lock.remove()
        assert second.ok is True
        assert second.existed is True
        assert not lock.path.exists()

        # And again — still no error.
        third = lock.remove()
        assert third.ok is True
        assert third.existed is False

    def test_write_is_atomic(self, tmp_path: Path) -> None:
        """After a write, no ``.tmp`` sibling should be left behind —
        ``os.replace`` renames the tempfile onto the target."""
        lock = Lockfile(tmp_path)
        payload = LockfilePayload.now(pid=1, port=1, wire_version="x")
        lock.write(payload)
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


class TestLockfileResults:
    """Coverage for the write/remove result-envelope failure paths.

    Pattern 3 (expected failures returned as values, not raised)
    only holds if callers can observe the failure — these tests
    pin that the envelope actually flips ``ok`` / carries a
    ``reason`` string when the FS refuses the operation.
    """

    def test_write_failure_on_readonly_dir_returns_not_ok(self, tmp_path: Path) -> None:
        """Writing under a read-only parent flips ``ok=False`` with
        the OS's error string in ``reason`` — no ``OSError`` bubbles
        out of :meth:`Lockfile.write`."""
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere; skip the permission-refusal path")

        parent = tmp_path / "locked"
        parent.mkdir()
        parent.chmod(0o400)  # read + no-write for owner
        try:
            lock = Lockfile(parent)
            payload = LockfilePayload.now(pid=1, port=1, wire_version="x")
            result = lock.write(payload)
            assert isinstance(result, WriteLockfileResult)
            assert result.ok is False
            assert result.reason is not None
            assert result.payload is None
        finally:
            # Restore perms so pytest can clean up.
            parent.chmod(0o700)

    def test_remove_missing_file_reports_not_existed(self, tmp_path: Path) -> None:
        """No file → ``ok=True, existed=False``."""
        lock = Lockfile(tmp_path)
        result = lock.remove()
        assert result.ok is True
        assert result.existed is False
        assert result.reason is None


class TestLockfilePayload:
    """Unit coverage for the payload's bound helpers — replaces the
    previous free-function ``is_pid_alive`` / ``is_port_reachable``
    tests. Same behaviour, class-scoped."""

    def test_matches_version_is_exact_string(self) -> None:
        p = LockfilePayload(pid=1, port=1, wire_version="0.9.1", created_at=0)
        assert p.matches_version("0.9.1") is True
        assert p.matches_version("0.9.2") is False
        assert p.matches_version("0.9.1 ") is False

    def test_age_seconds_uses_injected_now(self) -> None:
        p = LockfilePayload(pid=1, port=1, wire_version="x", created_at=1000)
        assert p.age_seconds(now=1042.5) == 42
        # Clock skew backwards should not return a negative age.
        assert p.age_seconds(now=500) == 0

    def test_current_process_is_alive(self) -> None:
        p = LockfilePayload(pid=os.getpid(), port=1, wire_version="x", created_at=0)
        assert p.is_pid_alive() is True

    def test_impossible_pid_is_dead(self) -> None:
        for pid in (0, -1):
            p = LockfilePayload(pid=pid, port=1, wire_version="x", created_at=0)
            assert p.is_pid_alive() is False

    def test_far_future_pid_is_dead(self) -> None:
        p = LockfilePayload(pid=2**31 - 1, port=1, wire_version="x", created_at=0)
        assert p.is_pid_alive() is False

    def test_unlikely_port_unreachable(self) -> None:
        p = LockfilePayload(pid=1, port=0, wire_version="x", created_at=0)
        assert p.is_port_reachable(timeout=0.1) is False

    def test_live_port_reachable(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            p = LockfilePayload(pid=1, port=port, wire_version="x", created_at=0)
            assert p.is_port_reachable(timeout=0.5) is True
        finally:
            srv.close()


class TestBackendDiscovery:
    """Coordinator-level tests. Probes are injected as fakes so we
    never touch real PIDs / TCP sockets unless the test genuinely
    needs the default probe (:class:`TestLockfilePayload` covers
    those in isolation)."""

    def test_no_lockfile_returns_nobackend(self, tmp_path: Path) -> None:
        discovery = BackendDiscovery(tmp_path, expected_wire_version="0.9.1")
        result = discovery.probe()
        assert isinstance(result, NoBackend)
        assert result.status == DiscoveryOutcome.NONE

    def test_stale_dead_pid_lock_is_removed(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.write(LockfilePayload.now(pid=1, port=1, wire_version="0.9.1"))
        assert lock.path.exists()

        discovery = BackendDiscovery(
            tmp_path,
            expected_wire_version="0.9.1",
            pid_probe=lambda _payload: False,
            port_probe=lambda _payload: True,
        )
        result = discovery.probe()
        assert isinstance(result, NoBackend)
        assert not lock.path.exists()

    def test_alive_pid_but_dead_port_lock_is_removed(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.write(LockfilePayload.now(pid=1, port=1, wire_version="0.9.1"))

        discovery = BackendDiscovery(
            tmp_path,
            expected_wire_version="0.9.1",
            pid_probe=lambda _payload: True,
            port_probe=lambda _payload: False,
        )
        result = discovery.probe()
        assert isinstance(result, NoBackend)
        assert not lock.path.exists()

    def test_version_mismatch_keeps_lockfile_and_signals(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.write(LockfilePayload.now(pid=1, port=1, wire_version="0.8.0"))

        discovery = BackendDiscovery(
            tmp_path,
            expected_wire_version="0.9.1",
            pid_probe=lambda _payload: True,
            port_probe=lambda _payload: True,
        )
        result = discovery.probe()
        assert isinstance(result, VersionMismatch)
        assert result.status == DiscoveryOutcome.VERSION_MISMATCH
        assert result.expected == "0.9.1"
        assert result.payload.wire_version == "0.8.0"
        # Lockfile intact — the running BE is legitimately owned;
        # only the caller-facing notification changes.
        assert lock.path.exists()

    def test_healthy_lock_returns_livebackend(self, tmp_path: Path) -> None:
        lock = Lockfile(tmp_path)
        lock.write(LockfilePayload.now(pid=1, port=9999, wire_version="0.9.1"))

        discovery = BackendDiscovery(
            tmp_path,
            expected_wire_version="0.9.1",
            pid_probe=lambda _payload: True,
            port_probe=lambda _payload: True,
        )
        result = discovery.probe()
        assert isinstance(result, LiveBackend)
        assert result.status == DiscoveryOutcome.LIVE
        assert result.payload.pid == 1
        assert result.payload.port == 9999
        assert result.payload.wire_version == "0.9.1"

    def test_default_probes_used_when_not_injected(self, tmp_path: Path) -> None:
        """No injected probes → the coordinator's staticmethod defaults
        fire. We use the current-process PID + a real bound port so
        the defaults return ``True`` without touching the network
        beyond loopback."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            lock = Lockfile(tmp_path)
            lock.write(LockfilePayload.now(pid=os.getpid(), port=port, wire_version="0.9.1"))

            discovery = BackendDiscovery(tmp_path, expected_wire_version="0.9.1")
            result = discovery.probe()
            assert isinstance(result, LiveBackend)
        finally:
            srv.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
