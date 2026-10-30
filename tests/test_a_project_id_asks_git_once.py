"""A project's id asks git once per repository state.

Every store that opens a scope asks for the project's id, and the id
comes from ``git remote get-url origin`` — a subprocess. ``ProjectIds``
keeps it until the repository's ``.git`` or its ``config`` changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ember_code.core.data_paths import ProjectIds


class GitCalls:
    def __init__(self, monkeypatch) -> None:
        self.count = 0
        ask = ProjectIds._ask_git

        def counted(project: Path) -> str:
            self.count += 1
            return ask(project)

        monkeypatch.setattr(ProjectIds, "_ask_git", staticmethod(counted))


def _repo(path: Path, remote: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


def test_a_project_id_asks_git_once(tmp_path: Path, monkeypatch):
    calls = GitCalls(monkeypatch)
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", "https://example.test/a.git")
    first = ids.of(project)
    assert all(ids.of(project) == first for _ in range(10))
    assert ids.of(project / "sub" / "..") == first
    assert calls.count == 1


def test_a_changed_remote_is_seen(tmp_path: Path, monkeypatch):
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", None)
    by_path = ids.of(project)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://example.test/b.git"],
        check=True,
    )
    by_remote = ids.of(project)
    assert by_remote != by_path
    other_clone = _repo(tmp_path / "clone", "https://example.test/b.git")
    assert ids.of(other_clone) == by_remote, "two clones, one id"


def test_a_folder_inside_a_repository_follows_its_config(tmp_path: Path):
    ids = ProjectIds()
    project = _repo(tmp_path / "repo", None)
    inside = project / "packages" / "app"
    inside.mkdir(parents=True)
    before = ids.of(inside)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://example.test/c.git"],
        check=True,
    )
    assert ids.of(inside) != before


def test_outside_any_repository_the_path_is_the_id(tmp_path: Path, monkeypatch):
    calls = GitCalls(monkeypatch)
    ids = ProjectIds()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ids.of(plain) == ids.of(plain)
    assert calls.count == 1
