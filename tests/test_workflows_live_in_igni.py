"""Workflows live in ``.igni/workflows``; ``.claude/workflows`` is a fallback.

The app-test sweep found ``run_workflow`` refusing an unknown name with
"not found in <project>/.claude/workflows" — the sibling tool's directory,
named as if it were igni's. Workflows are now read, in order (later wins):

1. ``~/.igni/workflows`` — the person's own, in every project;
2. the organisation's group policy cache;
3. ``<project>/.claude/workflows`` — still read, once noted as deprecated;
4. ``<project>/.igni/workflows`` — the project's, committed with it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import ember_code.backend.workflow_runner as runner_mod
from ember_code.backend.workflow_runner import WorkflowDiscovery, WorkflowRunner
from ember_code.core.paths import CONFIG_DIR


def _fake_exec_reporting_source(project: Path):
    """A discovery subprocess whose meta names the file's first line."""

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = Path(args[2])
        label = path.read_text().strip() or path.stem
        line = json.dumps(
            {
                "ts": 0,
                "run_id": "discovery",
                "seq": 0,
                "type": "workflow_meta",
                "payload": {"meta": {"name": label, "phases": []}},
            }
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=((line + "\n").encode("utf-8"), b""))
        proc.returncode = 0
        return proc

    return fake_exec


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(autouse=True)
def _reset_deprecation_note():
    runner_mod.WorkflowDiscovery._claude_dir_noted = False
    yield
    runner_mod.WorkflowDiscovery._claude_dir_noted = False


async def test_not_found_names_the_igni_directory(tmp_path: Path) -> None:
    runner = WorkflowRunner(project_dir=tmp_path, push=AsyncMock())
    with pytest.raises(FileNotFoundError) as caught:
        await runner.run(name="no-such-workflow", args={}, session=AsyncMock())
    message = str(caught.value)
    assert str(tmp_path / CONFIG_DIR / "workflows") in message
    assert ".claude" not in message


async def test_igni_workflows_win_over_claude_workflows(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / ".claude" / "workflows" / "shared.mjs", "claude-version")
    _write(tmp_path / CONFIG_DIR / "workflows" / "shared.mjs", "igni-version")
    monkeypatch.setattr(
        runner_mod.asyncio, "create_subprocess_exec", _fake_exec_reporting_source(tmp_path)
    )

    disc = WorkflowDiscovery(project_dir=tmp_path)
    found = await disc.resolve("shared")

    assert found is not None
    assert found.meta.name == "igni-version"
    assert found.path == f"{CONFIG_DIR}/workflows/shared.mjs"
    assert disc.workflows_dir == tmp_path / CONFIG_DIR / "workflows"


async def test_claude_workflows_still_read_with_one_deprecation_note(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _write(tmp_path / ".claude" / "workflows" / "old.mjs", "old")
    monkeypatch.setattr(
        runner_mod.asyncio, "create_subprocess_exec", _fake_exec_reporting_source(tmp_path)
    )

    disc = WorkflowDiscovery(project_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger=runner_mod.__name__):
        first = await disc.list_workflows()
        await disc.list_workflows()
        await WorkflowDiscovery(project_dir=tmp_path).list_workflows()

    assert [env.meta.name for env in first] == ["old"]
    notes = [r for r in caplog.records if ".claude/workflows" in r.getMessage()]
    assert len(notes) == 1
    assert f"{CONFIG_DIR}/workflows" in notes[0].getMessage()


async def test_user_level_workflows_are_read_and_project_shadows_them(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write(home / CONFIG_DIR / "workflows" / "mine.mjs", "mine")
    _write(home / CONFIG_DIR / "workflows" / "shared.mjs", "user-version")
    _write(project / CONFIG_DIR / "workflows" / "shared.mjs", "project-version")
    monkeypatch.setattr(
        runner_mod.asyncio, "create_subprocess_exec", _fake_exec_reporting_source(project)
    )

    disc = WorkflowDiscovery(project_dir=project, home_dir=home)
    names = [env.meta.name for env in await disc.list_workflows()]
    mine = await disc.resolve("mine")

    assert names == ["mine", "project-version"]
    assert mine is not None
    # Outside the project, so the path is absolute — the runner joins it
    # onto the project directory, which leaves an absolute path as it is.
    assert Path(mine.path) == home / CONFIG_DIR / "workflows" / "mine.mjs"
    assert project / mine.path == home / CONFIG_DIR / "workflows" / "mine.mjs"
