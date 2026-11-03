"""``.igni/.gitignore`` keeps the personal and runtime files out of git, and only those.

A fresh clone showed the first run writing ``.igni/`` into the repository with
nothing ignored — ``agents.tmp/``, ``.initialized``, ``.checksums.json`` all
untracked noise — and the obvious fix, ignoring ``.igni/`` wholesale, is wrong:
teams commit their shared agents, skills, hooks, workflows and config there.

So igni keeps a marked block in ``.igni/.gitignore`` that ignores only what is
one person's or one run's: ``*.local`` overrides, ephemeral agents, databases,
credentials, trust decisions, logs, attachments, igni's own bookkeeping and the
group policy pulled from the server. It rewrites only its block, so a line the
team adds outside it survives, and it writes nothing in a folder nobody trusted.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ember_code.core.init.gitignore import BLOCK_BEGIN, BLOCK_END, IgniGitignore
from ember_code.core.init.project_initializer import ProjectInitializer
from ember_code.core.paths import CONFIG_DIR

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

#: What a team shares through git, under ``.igni/``.
SHARED = [
    "agents/reviewer.md",
    "skills/deploy/SKILL.md",
    "hooks/pre-commit.sh",
    "workflows/release.mjs",
    "output-styles/terse.md",
    "config.yaml",
    "settings.json",
    "rules.md",
    ".mcp.json",
    "plugins.json",
    "tools/lint.py",
    "evals/smoke.yaml",
    "TODO.md",
]

#: What is one person's or one run's, under ``.igni/``.
RUNTIME = [
    "agents.local/mine.md",
    "skills.local/mine/SKILL.md",
    "settings.local.json",
    "config.local.yaml",
    "agents.tmp/scratch.md",
    "sessions.db",
    "memory.db",
    "state.db",
    "ember.db-wal",
    "credentials.json",
    "workspace-trust.json",
    "mcp-approved.json",
    "mcp-tool-state.json",
    "audit.log",
    "debug.log",
    "logs/backend.log",
    "process_logs/p1.log",
    "attachments/a.png",
    "group-policy/agents/org.md",
    "agents/reviewer.md.new",
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _init(repo: Path, tmp_path: Path) -> None:
    ProjectInitializer(project_dir=repo, home_ember=tmp_path / "home" / CONFIG_DIR).run()


def _untracked(repo: Path) -> set[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return {line[3:] for line in out.splitlines()}


def test_shared_files_show_and_runtime_files_do_not(tmp_path: Path):
    repo = _repo(tmp_path)
    _init(repo, tmp_path)
    for rel in [*SHARED, *RUNTIME]:
        path = repo / CONFIG_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    seen = _untracked(repo)

    for rel in SHARED:
        assert f"{CONFIG_DIR}/{rel}" in seen, rel
    for rel in RUNTIME:
        assert f"{CONFIG_DIR}/{rel}" not in seen, rel
    # igni's own bookkeeping from the init run itself.
    assert f"{CONFIG_DIR}/.initialized" not in seen
    assert f"{CONFIG_DIR}/.checksums.json" not in seen
    # The ignore file is shared too: the team gets the same list.
    assert f"{CONFIG_DIR}/.gitignore" in seen


def test_credentials_are_never_trackable(tmp_path: Path):
    repo = _repo(tmp_path)
    _init(repo, tmp_path)
    creds = repo / CONFIG_DIR / "credentials.json"
    creds.write_text("{}")

    assert _git(repo, "check-ignore", f"{CONFIG_DIR}/credentials.json").strip()
    # Not even with a negation in the repository's root .gitignore: the
    # deeper file wins.
    (repo / ".gitignore").write_text(f"!{CONFIG_DIR}/credentials.json\n")
    assert f"{CONFIG_DIR}/credentials.json" not in _untracked(repo)


def test_a_line_outside_the_block_survives_a_second_run(tmp_path: Path):
    repo = _repo(tmp_path)
    _init(repo, tmp_path)
    path = repo / CONFIG_DIR / ".gitignore"
    path.write_text("# ours\nscratch/\n" + path.read_text() + "\nlocal-notes.md\n")
    # Something inside the block is igni's to put back.
    path.write_text(path.read_text().replace("credentials.json\n", ""))

    _init(repo, tmp_path)
    text = path.read_text()

    assert text.startswith("# ours\nscratch/\n")
    assert text.rstrip().endswith("local-notes.md")
    assert text.count(BLOCK_BEGIN) == 1 and text.count(BLOCK_END) == 1
    assert "credentials.json\n" in text


def test_a_second_run_changes_nothing(tmp_path: Path):
    repo = _repo(tmp_path)
    _init(repo, tmp_path)
    path = repo / CONFIG_DIR / ".gitignore"
    before = path.read_text()
    mtime = path.stat().st_mtime_ns

    _init(repo, tmp_path)

    assert path.read_text() == before
    assert path.stat().st_mtime_ns == mtime


def test_a_block_without_its_end_is_repaired_not_duplicated(tmp_path: Path):
    config_dir = tmp_path / CONFIG_DIR
    config_dir.mkdir()
    path = config_dir / ".gitignore"
    path.write_text(f"mine/\n{BLOCK_BEGIN}\n*.db\n")

    IgniGitignore(config_dir).ensure()
    text = path.read_text()

    assert text.startswith("mine/\n")
    assert text.count(BLOCK_BEGIN) == 1 and text.count(BLOCK_END) == 1


@pytest.mark.workspace_trust
def test_nothing_is_written_in_an_untrusted_folder(tmp_path: Path):
    repo = _repo(tmp_path)
    _init(repo, tmp_path)

    assert not (repo / CONFIG_DIR / ".gitignore").exists()
