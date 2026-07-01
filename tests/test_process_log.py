"""Per-pid log files for backgrounded shell processes.

The headline contract this guards: a backgrounded process's
stdout survives a BE restart. Before the log files existed,
``_OrphanProcess.read()`` returned a placeholder explaining
that "live output can't be reattached"; now it returns the
actual content the previous BE captured.

Three layers:

* ``open_log`` / ``tail`` / ``cleanup`` — the on-disk side.
  Append-only writes, tail-from-end reads, idempotent delete.
* The reader task in ``shell._reader`` tees each line to the
  log alongside the in-memory buffer.
* ``_OrphanProcess.read`` reads from the file when the in-
  memory buffer is gone, with a graceful empty-file fallback.

We DON'T test the reader-tees path here at the unit level —
that requires running an actual subprocess + reader task,
which is the job of the broader shell-tool integration suite.
This file pins the log-files primitive + the orphan's use of
it.
"""

from __future__ import annotations

import time
from pathlib import Path

from ember_code.core.tools import process_log
from ember_code.core.tools.shell import _OrphanProcess


class TestLogPathResolution:
    def test_path_per_project(self, tmp_path: Path) -> None:
        p = process_log.log_path(42, tmp_path)
        assert p == tmp_path / ".ember" / "process_logs" / "42.log"

    def test_path_falls_back_to_tmp_when_no_project(self) -> None:
        # Tests + headless callers without a project root still
        # get a working path so the reader doesn't error.
        p = process_log.log_path(99, None)
        assert "ember-process-logs" in str(p)
        assert p.name == "99.log"

    def test_path_coerces_pid_to_int(self, tmp_path: Path) -> None:
        # Calling code (`open_log`, `cleanup`) passes pids
        # straight through — guard against accidental strings.
        p = process_log.log_path("42", tmp_path)  # type: ignore[arg-type]
        assert p.name == "42.log"


class TestOpenLog:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        # The project may not have an ``.ember/process_logs/``
        # subtree yet. The opener creates it as needed.
        assert not (tmp_path / ".ember").exists()
        f = process_log.open_log(123, tmp_path)
        assert f is not None
        f.write("hello\n")
        f.close()
        assert (tmp_path / ".ember" / "process_logs" / "123.log").exists()

    def test_append_mode_preserves_prior_content(self, tmp_path: Path) -> None:
        # A pid that's gone through eviction + reuse would clobber
        # the previous content if we opened in write mode. Append
        # is safer; cleanup is the explicit "I want to forget"
        # path.
        path = process_log.log_path(7, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("first\n")

        f = process_log.open_log(7, tmp_path)
        assert f is not None
        f.write("second\n")
        f.close()

        assert path.read_text() == "first\nsecond\n"

    def test_open_failure_returns_none(self, tmp_path: Path) -> None:
        # An unwritable path returns ``None``; the reader's
        # ``if self._log_file is None`` branch handles the
        # fallback. We can't easily produce "unwritable" on the
        # test FS, so spin a file with a name that collides with
        # an existing directory — the open raises IsADirectoryError
        # which the OSError catch swallows.
        path = process_log.log_path(8, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()  # the supposed log file is now a directory

        result = process_log.open_log(8, tmp_path)
        assert result is None


class TestTail:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        # ``_OrphanProcess.read`` calls tail before checking file
        # existence — the helper has to handle the gap.
        out = process_log.tail(tmp_path / "nonexistent.log", n=10)
        assert out == ""

    def test_returns_last_n_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "x.log"
        path.write_text("a\nb\nc\nd\ne\n")
        assert process_log.tail(path, n=3) == "c\nd\ne"

    def test_tail_n_greater_than_lines_returns_all(self, tmp_path: Path) -> None:
        path = tmp_path / "x.log"
        path.write_text("only\ntwo\n")
        assert process_log.tail(path, n=100) == "only\ntwo"

    def test_tail_zero_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "x.log"
        path.write_text("some content\n")
        assert process_log.tail(path, n=0) == ""

    def test_tail_strips_trailing_newline_for_clean_render(self, tmp_path: Path) -> None:
        # The watcher renders the tail in a ``<pre>``; a trailing
        # newline would push the bottom anchor down a row.
        path = tmp_path / "x.log"
        path.write_text("one\ntwo\n")
        assert process_log.tail(path, n=10).endswith("two")


class TestCleanup:
    def test_removes_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / ".ember" / "process_logs" / "55.log"
        path.parent.mkdir(parents=True)
        path.write_text("doomed\n")

        process_log.cleanup(55, tmp_path)

        assert not path.exists()

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        # Eviction TTL fires regardless of whether a log was
        # ever written — most short-lived backgrounded
        # processes produce no output. Cleanup must not raise.
        process_log.cleanup(99999, tmp_path)


class TestOrphanRead:
    """``_OrphanProcess.read`` is what the watcher panel calls
    when the user expands an orphan row. Three cases matter:

    * Log file exists with content → return the tail.
    * Log file doesn't exist (lost to TTL, never written) →
      return the helpful placeholder.
    * Log file exists but empty → also return the placeholder
      so the watcher doesn't show a blank pane that looks like
      a render bug.
    """

    def setup_method(self) -> None:
        # Reset module-global so test order doesn't matter.
        process_log.set_default_project_dir(None)

    def test_reads_from_log_file(self, tmp_path: Path) -> None:
        # Seed a log file as if a previous BE wrote it.
        process_log.set_default_project_dir(tmp_path)
        log = process_log.log_path(101, tmp_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[19:00:01] starting server\n[19:00:02] listening on :3000\n")

        orphan = _OrphanProcess(
            pid=101, cmd="npm run dev", started_epoch=int(time.time()), pgid=101
        )
        out = orphan.read(tail=10)
        assert "starting server" in out
        assert "listening on :3000" in out

    def test_missing_log_returns_placeholder(self, tmp_path: Path) -> None:
        process_log.set_default_project_dir(tmp_path)
        orphan = _OrphanProcess(
            pid=202, cmd="no-output-yet", started_epoch=int(time.time()) - 5, pgid=None
        )
        out = orphan.read()
        assert "no buffered output" in out.lower()
        assert "kill button" in out.lower()

    def test_empty_log_returns_placeholder(self, tmp_path: Path) -> None:
        # A backgrounded process can be totally silent — its log
        # file gets created (we open append on first line) but no
        # writes land. After restart the orphan finds the empty
        # file and should fall through to the placeholder.
        process_log.set_default_project_dir(tmp_path)
        log = process_log.log_path(303, tmp_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()

        orphan = _OrphanProcess(pid=303, cmd="silent", started_epoch=int(time.time()), pgid=None)
        out = orphan.read()
        assert "no buffered output" in out.lower()

    def test_read_respects_tail_count(self, tmp_path: Path) -> None:
        # The watcher's ``read_process_tail`` RPC passes a tail
        # value through — the orphan's read must honor it so a
        # huge log doesn't slurp into the FE on every open.
        process_log.set_default_project_dir(tmp_path)
        log = process_log.log_path(404, tmp_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n")

        orphan = _OrphanProcess(pid=404, cmd="chatty", started_epoch=int(time.time()), pgid=None)
        out = orphan.read(tail=5)
        lines = out.splitlines()
        # Tail of last 5 — first should be ``line 995``.
        assert lines[0] == "line 995"
        assert lines[-1] == "line 999"


class TestProjectDirWiring:
    """``set_default_project_dir`` is the one-shot setter
    BackendServer.startup calls. Tests round-trip the value
    and verify the path resolver uses it."""

    def test_set_then_get_round_trips(self, tmp_path: Path) -> None:
        process_log.set_default_project_dir(tmp_path)
        assert process_log.get_default_project_dir() == tmp_path

    def test_orphan_read_uses_default_project_dir(self, tmp_path: Path) -> None:
        # If the orphan's project_dir lookup misuses ``log_path``,
        # the read goes to TMPDIR and we get back the placeholder
        # even though the real log file exists. Pin the wiring.
        process_log.set_default_project_dir(tmp_path)
        log = process_log.log_path(555, tmp_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("real output\n")

        orphan = _OrphanProcess(pid=555, cmd="x", started_epoch=int(time.time()), pgid=None)
        assert "real output" in orphan.read()
