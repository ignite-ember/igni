"""Installing a backend late must reach the running agent.

Both neo4j attach points run after the Session is built — the BE's
orchestrator calls them once its runtime is up — and both changed a
field the agent had already been built from:

* ``attach_knowledge_neo4j`` installed ``self.knowledge`` but left the
  pool holding the ``None`` it was given at construction and the main
  team holding the toolkit it was built with. Since PRD-22 made the
  knowledge prompt block conditional on ``s.knowledge is not None``, the
  model was told nothing about the index either — ``KnowledgeTools``
  attached to nothing.
* ``attach_codeindex_neo4j`` wrote ``_codeindex_available`` by hand and
  dropped the ``on_sync_complete`` callback the constructor wires. The
  flag decides the tool list and the prompt variant AT BUILD TIME, so
  the session claimed CodeIndex was available while the running agent
  held ``main_agent.md`` and a tool list without it — and no later sync
  could correct it, because nothing was listening.

Both are pinned here through the agent the user actually talks to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.paths import CONFIG_DIR

_STUB_MODEL: dict[str, Any] = {
    "default": "stub",
    "registry": {
        "stub": {
            "provider": "openai_like",
            "model_id": "stub-1",
            "url": "http://localhost:9/v1",
            "api_key": "x",
            "context_window": 8192,
        }
    },
}


def _repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _settings(project: Path, *, home: Path, codeindex: bool, knowledge: bool) -> Settings:
    config = project / CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "code_index": {"enabled": codeindex},
                "knowledge": {"enabled": knowledge},
                "models": _STUB_MODEL,
                "storage": {"data_dir": str(home)},
            }
        )
    )
    return load_settings(project_dir=project)


def _tool_names(session) -> list[str]:
    return [type(t).__name__ for t in (session.main_team.tools or [])]


# ── Knowledge ────────────────────────────────────────────────


class _StubKnowledgeClient:
    def __init__(self, driver, project_id) -> None:  # noqa: ANN001
        self._driver = driver
        self.project_id = project_id

    async def apply_schema(self) -> None:
        return None


class _StubKnowledgeIndex:
    def __init__(self, **kwargs: Any) -> None:
        self._neo4j_client = kwargs.get("neo4j_client")

    async def start(self) -> None:
        return None


class _StubRuntime:
    """Only the protocol surface ``attach_knowledge_neo4j`` depends on."""

    def __init__(self) -> None:
        self.driver = object()

    def driver_for_knowledge(self, project_id: str):  # noqa: ANN201
        return self.driver


# ``test_attaching_knowledge_puts_the_tools_on_the_running_agent`` came from
# a branch where the main agent held the knowledge tools. Here only the
# archivist holds the artifact toolkit (agents/builder.py), so there is no
# main-agent tool to appear after a late attach.


# ── CodeIndex ────────────────────────────────────────────────


class _StubCodeIndex:
    """An index that says HEAD is covered, so availability flips on."""

    def __init__(self, **kwargs: Any) -> None:
        self._neo4j_runtime = kwargs.get("runtime")

    def has_commit(self, sha: str) -> bool:
        return True


class _StubSyncManager:
    #: The ``on_sync_complete`` callback each constructed manager was handed.
    seen: list[Any] = []

    def __init__(self, on_sync_complete=None) -> None:  # noqa: ANN001
        self.on_sync_complete = on_sync_complete
        _StubSyncManager.seen.append(on_sync_complete)

    @classmethod
    def from_settings(cls, settings, **kwargs: Any):  # noqa: ANN001, ANN206
        return cls(on_sync_complete=kwargs.get("on_sync_complete"))

    def current_sha(self) -> str:
        return "deadbeef"


@pytest.mark.asyncio
async def test_attaching_codeindex_rebuilds_the_agent_around_the_new_flag(
    tmp_path: Path, monkeypatch
) -> None:
    from ember_code.core.session.core import Session

    project = _repo(tmp_path / "proj")
    session = Session(
        settings=_settings(project, home=tmp_path / "home", codeindex=True, knowledge=False),
        project_dir=project,
    )
    # Nothing is indexed yet, so the agent booted without CodeIndex.
    assert session._codeindex_available is False
    assert "CodeIndexTools" not in _tool_names(session)

    # Patched after construction so the session under test is the real
    # one; only the replacement backend is a stub.
    _StubSyncManager.seen = []
    monkeypatch.setattr("ember_code.core.session.core.CodeIndex", _StubCodeIndex)
    monkeypatch.setattr("ember_code.core.session.core.CodeIndexSyncManager", _StubSyncManager)
    monkeypatch.setattr("ember_code.core.session.core.LiveEmbedder", lambda *a, **k: object())

    await session.attach_codeindex_neo4j(object())

    assert session._codeindex_available is True
    assert "CodeIndexTools" in _tool_names(session), (
        "the flag flipped but the agent still holds the tool list it was built "
        "with — _resolve_main_tool_names only appends CodeIndex at build time"
    )
    # And the replacement sync manager can still tell the session when a
    # background sync lands; without the callback nothing else can.
    assert _StubSyncManager.seen and _StubSyncManager.seen[-1] is not None


@pytest.mark.asyncio
async def test_attach_honours_the_codeindex_kill_switch(tmp_path: Path, monkeypatch) -> None:
    # The control: availability must not come back on for a session an
    # admin turned CodeIndex off for. The bare write ignored the flag.
    from ember_code.core.session.core import Session

    project = _repo(tmp_path / "proj")
    session = Session(
        settings=_settings(project, home=tmp_path / "home", codeindex=False, knowledge=False),
        project_dir=project,
    )
    monkeypatch.setattr("ember_code.core.session.core.CodeIndex", _StubCodeIndex)
    monkeypatch.setattr("ember_code.core.session.core.CodeIndexSyncManager", _StubSyncManager)
    monkeypatch.setattr("ember_code.core.session.core.LiveEmbedder", lambda *a, **k: object())

    await session.attach_codeindex_neo4j(object())

    assert session._codeindex_available is False
