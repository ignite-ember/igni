"""The DCO check runs on every pull request, and its script does what it says.

CONTRIBUTING.md asks for a ``Signed-off-by`` line on every commit. The check
in ``.github/workflows/dco.yml`` enforces it; these tests run that check's
own shell script against a scratch repository, so a regex or range change
that quietly passes everything fails here first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_DCO = _ROOT / ".github" / "workflows" / "dco.yml"
_CONTRIBUTING = _ROOT / "CONTRIBUTING.md"


def _workflow() -> dict:
    return yaml.safe_load(_DCO.read_text())


def _check_step() -> dict:
    steps = _workflow()["jobs"]["signed-off"]["steps"]
    return next(s for s in steps if s.get("name") == "Check Signed-off-by")


def test_it_runs_on_pull_requests_without_secrets():
    workflow = _workflow()
    # PyYAML reads the bare key ``on`` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert "if" not in workflow["jobs"]["signed-off"], "igni is the public repo: it runs here"


def test_every_action_is_pinned_by_sha():
    for step in _workflow()["jobs"]["signed-off"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+", _DCO.read_text())


def test_contributing_says_how_to_sign_off_and_fix_a_missed_one():
    text = _CONTRIBUTING.read_text()
    assert "git commit -s" in text
    assert "git commit --amend -s --no-edit" in text
    assert "git rebase --signoff main" in text
    assert ".github/workflows/dco.yml" in text
    assert "not yet enabled" not in text


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@example.test",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "a@example.test",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_check(
    repo: Path, base: str, head: str, signed_off_from: str
) -> subprocess.CompletedProcess:
    step = _check_step()
    env = {**os.environ, "BASE": base, "HEAD": head, "SIGNED_OFF_FROM": signed_off_from}
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("needs git and bash")
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_a_signed_off_branch_passes(repo: Path):
    base = _commit(repo, "base")
    head = _commit(repo, "change\n\nSigned-off-by: A <a@example.test>")
    result = _run_check(repo, base, head, "0" * 40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Every commit is signed off." in result.stdout


def test_a_commit_without_sign_off_fails_and_is_named(repo: Path):
    base = _commit(repo, "base")
    unsigned = _commit(repo, "change")
    head = _commit(repo, "fix\n\nSigned-off-by: A <a@example.test>")
    result = _run_check(repo, base, head, "0" * 40)
    assert result.returncode != 0
    assert unsigned in result.stdout
    assert "git rebase --signoff" in result.stdout


def test_history_before_the_check_existed_is_not_rechecked(repo: Path):
    """feat/extensions carries hundreds of commits from before the check, all
    one author's, none signed off. They are excluded by SHA; a commit after
    them is checked as usual."""
    base = _commit(repo, "base")
    _commit(repo, "old, unsigned")
    cutoff = _commit(repo, "last commit before the check, unsigned")
    head = _commit(repo, "new\n\nSigned-off-by: A <a@example.test>")
    assert _run_check(repo, base, head, cutoff).returncode == 0

    after = _commit(repo, "new, unsigned")
    result = _run_check(repo, base, after, cutoff)
    assert result.returncode != 0
    assert after in result.stdout


def test_the_cutoff_is_a_real_commit_of_this_repository():
    cutoff = _check_step()["env"]["SIGNED_OFF_FROM"]
    assert re.fullmatch(r"[0-9a-f]{40}", cutoff)
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{cutoff}^{{commit}}"], cwd=_ROOT, capture_output=True
    )
    if probe.returncode != 0 and not (_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    if probe.returncode != 0 and _git(_ROOT, "rev-parse", "--is-shallow-repository") == "true":
        pytest.skip("shallow clone does not reach the cutoff")
    assert probe.returncode == 0, f"{cutoff} is not in this repository's history"
