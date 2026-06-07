"""Direct unit tests for ``GitClient``.

The installer + marketplace tests exercise ``GitClient`` indirectly
via real git operations against local repos. These cover the error
paths and small utility methods that don't naturally surface from
the higher-level tests: timeout, ``git`` not on PATH, the
``head_branch`` fallback, etc.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ember_code.core.plugins.git import GitClient, GitError

# ── is_available ───────────────────────────────────────────────────


def test_is_available_true_when_git_present() -> None:
    """In any environment running these tests, git is on PATH (we
    install it as a test dep)."""
    assert GitClient().is_available() is True


def test_is_available_false_when_subprocess_fails() -> None:
    """A FileNotFoundError from subprocess (= git not on PATH) is
    swallowed by ``is_available`` so the installer's precondition
    check has a non-raising boolean to consult."""
    with patch(
        "ember_code.core.plugins.git.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        assert GitClient().is_available() is False


def test_is_available_false_on_nonzero_exit() -> None:
    """``check=True`` raises CalledProcessError on non-zero exit;
    is_available catches and returns False rather than letting the
    exception leak. Means a corrupted git install fails closed
    instead of breaking ember startup."""
    err = subprocess.CalledProcessError(1, ["git", "--version"])
    with patch(
        "ember_code.core.plugins.git.subprocess.run",
        side_effect=err,
    ):
        assert GitClient().is_available() is False


# ── _run error paths ──────────────────────────────────────────────


def test_run_raises_giterror_on_nonzero_exit() -> None:
    """Non-zero exit from git is wrapped in GitError carrying the
    verbatim stderr — the user sees exactly what git would have
    printed interactively."""
    result = subprocess.CompletedProcess(
        args=["git", "blah"],
        returncode=128,
        stdout="",
        stderr="fatal: not a git repository",
    )
    with (
        patch(
            "ember_code.core.plugins.git.subprocess.run",
            return_value=result,
        ),
        pytest.raises(GitError, match="not a git repository"),
    ):
        GitClient()._run(["git", "blah"])


def test_run_falls_back_to_stdout_when_stderr_empty() -> None:
    """If git prints the failure on stdout (some commands do), we
    surface that instead of an empty message."""
    result = subprocess.CompletedProcess(
        args=["git", "x"],
        returncode=1,
        stdout="from stdout",
        stderr="",
    )
    with (
        patch(
            "ember_code.core.plugins.git.subprocess.run",
            return_value=result,
        ),
        pytest.raises(GitError, match="from stdout"),
    ):
        GitClient()._run(["git", "x"])


def test_run_raises_giterror_on_timeout() -> None:
    """Timeouts become GitError with the seconds in the message —
    the install command surfaces this verbatim so the user can
    raise the timeout or check the network."""
    with (
        patch(
            "ember_code.core.plugins.git.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git", "x"], timeout=1),
        ),
        pytest.raises(GitError, match="timed out"),
    ):
        GitClient(timeout=1)._run(["git", "x"])


def test_run_raises_giterror_when_git_not_on_path() -> None:
    """A FileNotFoundError → GitError with an install hint. The
    installer's ``is_git_available`` precondition normally catches
    this first, but the wrapper itself must still fail cleanly when
    called directly."""
    with (
        patch(
            "ember_code.core.plugins.git.subprocess.run",
            side_effect=FileNotFoundError,
        ),
        pytest.raises(GitError, match="not found"),
    ):
        GitClient()._run(["git", "x"])


# ── head_branch fallback ──────────────────────────────────────────


def test_head_branch_falls_back_to_main_on_failure(tmp_path: Path) -> None:
    """If ``git symbolic-ref refs/remotes/origin/HEAD`` fails (no
    origin, dangling HEAD, etc.) we fall back to ``main`` rather
    than raise. This lets ``update`` pick a sensible default for
    plugins where the head branch hasn't been set explicitly."""
    # Use a non-git directory so symbolic-ref fails.
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    assert GitClient().head_branch(plain_dir) == "main"


# ── current_sha ───────────────────────────────────────────────────


def test_current_sha_returns_stripped_hash() -> None:
    """``git rev-parse HEAD`` includes a trailing newline; we strip
    it so callers get a clean SHA they can store directly."""
    result = subprocess.CompletedProcess(
        args=["git", "rev-parse", "HEAD"],
        returncode=0,
        stdout="abcdef1234\n",
        stderr="",
    )
    with patch(
        "ember_code.core.plugins.git.subprocess.run",
        return_value=result,
    ):
        assert GitClient().current_sha(Path("/tmp")) == "abcdef1234"
