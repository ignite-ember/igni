"""The first run in a folder writes no permissions the folder may not set.

Found on a fresh clone: the first ``igni -m`` in a new project wrote a
starter ``.igni/settings.local.json`` with a ``permissions`` block, and every
run after it printed — twice — that the file "sets permissions, which a
project may set only once you trust the folder; ignored". A new user's first
folder is an untrusted one, so the first thing igni told them about was a
file igni had written itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.init.project_initializer import ProjectInitializer
from ember_code.core.workspace_trust import WorkspaceTrust

pytestmark = pytest.mark.workspace_trust


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    return project


def test_an_untrusted_folder_gets_no_starter_permissions(tmp_path: Path):
    project = _project(tmp_path)

    ProjectInitializer.initialize(project)

    assert (project / ".igni" / "config.yaml").exists()  # the rest of the first run happened
    assert not (project / ".igni" / "settings.local.json").exists()


def test_a_trusted_folder_still_gets_the_starter(tmp_path: Path):
    project = _project(tmp_path)
    WorkspaceTrust.shared().grant_for_run(project)

    ProjectInitializer.initialize(project)

    assert (project / ".igni" / "settings.local.json").exists()
