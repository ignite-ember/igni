"""The session-level check that fails a run leaving files in the repository root."""

from __future__ import annotations

import os

from tests.root_guard import RootGuard


def test_a_new_file_is_a_stray(tmp_path):
    guard = RootGuard.taking(tmp_path)
    (tmp_path / "MagicMock").mkdir()
    (tmp_path / "<Engine object at 0x10f53c250>").write_text("x")

    assert guard.strays() == ["<Engine object at 0x10f53c250>", "MagicMock"]
    assert "MagicMock" in guard.report(guard.strays())


def test_a_tool_cache_is_not(tmp_path):
    guard = RootGuard.taking(tmp_path)
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".coverage.host.123").write_text("")

    assert guard.strays() == []


def test_an_old_leftover_written_again_is_a_stray(tmp_path):
    """A leftover from an old run must not hide the same leak recurring."""
    leftover = tmp_path / "MagicMock"
    leftover.mkdir()
    os.utime(leftover, (1, 1))
    (tmp_path / "README.md").write_text("old")
    os.utime(tmp_path / "README.md", (1, 1))
    guard = RootGuard.taking(tmp_path)

    (leftover / "id").write_text("x")
    (tmp_path / "README.md").write_text("edited by hand meanwhile")

    assert guard.strays() == ["MagicMock"]


def test_the_suite_is_guarded():
    from tests import conftest

    assert conftest.pytest_sessionfinish.__doc__
    assert conftest._ROOT.joinpath("pyproject.toml").is_file()
