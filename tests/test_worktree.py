"""Tests for worktree.py — git worktree lifecycle management."""

import subprocess
from pathlib import Path

import pytest

from ember_code.core.worktree import WorktreeManager, WorktreeRoot


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


class TestWorktreeManager:
    def test_validates_git_repo(self, tmp_path):
        with pytest.raises(RuntimeError, match="Not a git repository"):
            WorktreeManager(tmp_path)

    def test_creates_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="test-branch")

        assert info.worktree_path.exists()
        assert info.branch_name == "test-branch"
        assert info.original_dir == tmp_path.resolve()
        assert (info.worktree_path / "README.md").exists()

        # Cleanup
        wm.cleanup()

    def test_auto_generates_branch_name(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(session_id="abc123")

        assert info.branch_name == "ember-worktree-abc123"
        assert info.worktree_path.exists()

        wm.cleanup()

    def test_has_changes_false_on_clean(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        wm.create(branch_name="clean-test")

        assert not wm.has_changes()
        wm.cleanup()

    def test_has_changes_true_on_modified(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="dirty-test")

        # Make a change in the worktree
        (info.worktree_path / "new_file.txt").write_text("hello")
        assert wm.has_changes()

        # Force cleanup for test teardown
        subprocess.run(
            ["git", "worktree", "remove", str(info.worktree_path), "--force"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "dirty-test"],
            cwd=tmp_path,
            capture_output=True,
        )

    def test_cleanup_removes_clean_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="cleanup-test")

        assert info.worktree_path.exists()
        cleaned = wm.cleanup()
        assert cleaned.ok
        assert wm.info is None

    def test_cleanup_preserves_dirty_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        info = wm.create(branch_name="preserve-test")

        (info.worktree_path / "change.txt").write_text("data")
        cleaned = wm.cleanup()
        assert cleaned.status == "preserved_dirty"
        assert info.worktree_path.exists()

        # Force cleanup for test teardown
        subprocess.run(
            ["git", "worktree", "remove", str(info.worktree_path), "--force"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "preserve-test"],
            cwd=tmp_path,
            capture_output=True,
        )

    def test_error_on_duplicate_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        wm = WorktreeManager(tmp_path)
        wm.create(branch_name="dup-test")

        wm2 = WorktreeManager(tmp_path)
        with pytest.raises(RuntimeError, match="already exists"):
            wm2.create(branch_name="dup-test")

        wm.cleanup()


class TestCleanupStaleWorktrees:
    def test_prunes_stale_worktrees(self, tmp_path):
        _init_git_repo(tmp_path)
        # Just verify it doesn't crash on a clean repo
        result = WorktreeRoot().prune_stale(tmp_path)
        assert isinstance(result.empty_dirs_removed, list)
