"""Tests for the project-map persistence layer.

The map is **generated server-side**; the client just persists what
arrives in the JSONL changeset's ``CommitSummaryOp``. So the client
tests cover only:

  - Path computation (where on disk the map lives).
  - ``write_server_supplied_map`` — writes the markdown to disk
    verbatim, no transformation.
  - ``load_project_map`` — reads what was written, returns ``None``
    when missing or unreadable.
  - End-to-end: a ``CommitSummaryOp`` flowing through ``apply_delta``
    lands on disk at the expected path.

There are no LLM tests here — the rendering logic lives on the server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.core.code_index.delta import DeltaError, parse_op
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.manifest import (
    CommitInfo,
    Manifest,
    ManifestState,
)
from ember_code.core.code_index.project_map import (
    load_project_map,
    project_map_path,
    write_server_supplied_map,
)
from ember_code.core.config.settings import Settings

COMMIT = "a" * 40


# ── Path computation ────────────────────────────────────────────────


def test_project_map_path_sits_next_to_chroma(tmp_path: Path) -> None:
    """The map file lives in the same dir as the per-commit chroma so
    the two are paired (and can be GC'd together)."""
    path = project_map_path(tmp_path / "proj", COMMIT, data_dir=tmp_path / "ember")
    assert path.name == f"{COMMIT}.project_map.md"
    # Parent dir matches the codeindex layout: <data>/projects/<hash>/code_index/
    assert path.parent.name == "code_index"


# ── Write + load roundtrip ─────────────────────────────────────────


def test_write_server_supplied_map_persists_markdown_verbatim(tmp_path: Path) -> None:
    """Server-supplied markdown is written exactly as received — no
    post-processing, no validation, no truncation. The server is the
    canonical form; if it emitted garbage, surfacing that to the agent
    is the right thing so we notice upstream issues."""
    project = tmp_path / "proj"
    data_dir = tmp_path / "ember"
    body = (
        "## Project snapshot\n"
        "5 folders · 12 files · 80 entities indexed.\n\n"
        "## Tables\n- `app/db/users.py` `User` — auth user table.\n"
    )
    path = write_server_supplied_map(
        project=project,
        data_dir=data_dir,
        commit_sha=COMMIT,
        markdown=body,
    )
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == body


def test_load_project_map_returns_written_content(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    data_dir = tmp_path / "ember"
    body = "## Project snapshot\n\nroundtrip content\n"
    write_server_supplied_map(
        project=project,
        data_dir=data_dir,
        commit_sha=COMMIT,
        markdown=body,
    )
    assert load_project_map(project, COMMIT, data_dir=data_dir) == body


def test_load_project_map_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_project_map(tmp_path / "missing", "deadbeef") is None


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    """The codeindex dir doesn't exist yet when the first changeset
    is applied — write_server_supplied_map must mkdir as needed."""
    project = tmp_path / "fresh"
    data_dir = tmp_path / "ember"
    assert not (data_dir / "projects").exists()
    path = write_server_supplied_map(
        project=project,
        data_dir=data_dir,
        commit_sha=COMMIT,
        markdown="ok",
    )
    assert path.is_file()


# ── CommitSummaryOp parsing ─────────────────────────────────────────


def test_parse_commit_summary_op() -> None:
    """``parse_op`` recognises the new op kind and returns the right
    model — important so unknown-op handling doesn't accidentally
    swallow it as a skipped line."""
    raw = json.dumps(
        {
            "op": "commit_summary",
            "sha": COMMIT,
            "markdown": "## Project snapshot\n\nbody\n",
        }
    )
    op = parse_op(raw)
    assert op is not None
    assert op.op == "commit_summary"  # type: ignore[attr-defined]
    assert op.sha == COMMIT  # type: ignore[attr-defined]
    assert "Project snapshot" in op.markdown  # type: ignore[attr-defined]


def test_parse_commit_summary_op_validation() -> None:
    """Missing required fields surface as a parse error, not silently
    accepted with empty values."""
    raw = json.dumps({"op": "commit_summary", "sha": COMMIT})  # no markdown
    with pytest.raises(DeltaError):
        parse_op(raw)


# ── End-to-end: changeset → on-disk map ────────────────────────────


def _jsonl(*ops: dict) -> str:
    return "\n".join(json.dumps(o) for o in ops) + "\n"


@pytest.fixture
async def populated_index(tmp_path):
    """Fresh CodeIndex on a temp project so apply_delta has somewhere
    to land its commits."""
    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "ember"
    idx = CodeIndex(project=project, data_dir=data_dir)
    yield idx, project, data_dir
    await idx.close()


@pytest.mark.asyncio
async def test_apply_delta_writes_commit_summary_to_disk(
    populated_index,
    tmp_path: Path,
) -> None:
    """End-to-end: a JSONL changeset carrying a ``commit_summary`` op
    results in the markdown landing at the canonical path. The agent
    can then load it next session."""
    idx, project, data_dir = populated_index

    body = (
        "_Auto-generated for commit `aaaaaaaaaaaa`. Factual; no opinions._\n\n"
        "## Project snapshot\n5 folders · 0 files · 0 entities indexed.\n"
    )
    jsonl_path = tmp_path / "delta.jsonl"
    jsonl_path.write_text(
        _jsonl(
            {
                "op": "commit",
                "sha": COMMIT,
                "parent_sha": None,
                "branches": [],
                "indexed_at": "2026-01-01T00:00:00Z",
            },
            {"op": "commit_summary", "sha": COMMIT, "markdown": body},
        )
    )

    stats = await idx.apply_delta(jsonl_path)
    assert stats.commit_summary_written is True

    # File landed at the expected path with the verbatim body.
    loaded = load_project_map(project, COMMIT, data_dir=data_dir)
    assert loaded == body


@pytest.mark.asyncio
async def test_apply_delta_without_commit_summary_leaves_no_map(
    populated_index,
    tmp_path: Path,
) -> None:
    """Older changesets (or commits where the server didn't generate
    a map) don't leave behind a stale or empty file. The map slot
    stays absent and the session loader degrades gracefully."""
    idx, project, data_dir = populated_index
    jsonl_path = tmp_path / "delta.jsonl"
    jsonl_path.write_text(
        _jsonl(
            {
                "op": "commit",
                "sha": COMMIT,
                "parent_sha": None,
                "branches": [],
                "indexed_at": "2026-01-01T00:00:00Z",
            },
        )
    )

    stats = await idx.apply_delta(jsonl_path)
    assert stats.commit_summary_written is False
    assert load_project_map(project, COMMIT, data_dir=data_dir) is None


# ── Session loader: injection into the agent's system prompt ───────
#
# The loader hook at ``session/core.py:_build_main_agent`` reads the
# on-disk map and appends it to the assembled system prompt as
# ``## Project Map\n\n<md>``. The whole block is wrapped in a bare
# ``except Exception: pass`` — a regression there (renamed Manifest
# method, broken import, attribute typo) would silently drop the map
# from every session's prompt without any test catching it. These
# tests are the safety net for that path.


def _write_head_manifest(project: Path, data_dir: Path, head_sha: str) -> None:
    """Plant a real manifest.json pointing ``head`` at *head_sha*."""
    Manifest(project=project, data_dir=data_dir).save(
        ManifestState(
            head=head_sha,
            commits={head_sha: CommitInfo(sha=head_sha, last_used_at="2026-01-01T00:00:00+00:00")},
        )
    )


def _build_session_capture_prompt(
    project: Path, data_dir: Path, *, codeindex_available: bool = True
) -> str:
    """Spin up a Session under the shared Session-test mock stack and
    return the first instruction (the assembled system prompt) handed
    to ``Agent(...)``. The Session-test patches stub out everything
    heavy (DB, MCP, pool, model registry) so only the prompt-assembly
    path runs for real."""
    from tests.test_session import _session_patches

    patches = _session_patches()
    mocks = {p.attribute: p.start() for p in patches}
    try:
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        cc = mocks["CloudCredentials"].return_value
        cc.is_authenticated = False
        cc.access_token = None
        cc.org_id = None
        cc.org_name = None
        cc.email = None
        sync = mocks["CodeIndexSyncManager"].from_settings.return_value
        if codeindex_available:
            sync.current_sha.return_value = "deadbeef"
            mocks["CodeIndex"].return_value.has_commit.return_value = True
        else:
            sync.current_sha.return_value = None
        # ``SkillPool().describe()`` defaults to a MagicMock (truthy) which
        # would taint the prompt via str-concat. Force it to an empty
        # string so the only post-load_prompt mutation under test is the
        # Project Map injection.
        mocks["SkillPool"].return_value.describe.return_value = ""

        from ember_code.core.session.core import Session

        settings = Settings()
        settings.storage.data_dir = str(data_dir)
        Session(settings, project_dir=project)

        return mocks["Agent"].call_args[1]["instructions"][0]
    finally:
        for p in patches:
            p.stop()


def test_session_prompt_includes_project_map_when_present(tmp_path: Path) -> None:
    """Happy path: manifest head + .project_map.md on disk → the
    assembled system prompt carries the markdown verbatim under a
    ``## Project Map`` heading. This is the *only* way the server's
    rendered map reaches the agent; if this assertion ever flips, the
    whole feature is silently dead."""
    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "ember"
    _write_head_manifest(project, data_dir, COMMIT)
    write_server_supplied_map(
        project=project,
        data_dir=data_dir,
        commit_sha=COMMIT,
        markdown="## Project snapshot\n\n5 folders. taxonomy stuff.\n",
    )

    prompt = _build_session_capture_prompt(project, data_dir)

    assert "## Project Map" in prompt
    assert "5 folders. taxonomy stuff." in prompt


def test_session_prompt_omits_project_map_when_file_missing(tmp_path: Path) -> None:
    """Manifest has a head but no map file (older changeset, or the
    server's LLM render failed). The prompt builds cleanly without
    the ``## Project Map`` section — no crash, no empty placeholder."""
    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "ember"
    _write_head_manifest(project, data_dir, COMMIT)
    # No write_server_supplied_map call — the file slot is absent.

    prompt = _build_session_capture_prompt(project, data_dir)

    assert "## Project Map" not in prompt


def test_session_prompt_omits_project_map_when_codeindex_unavailable(
    tmp_path: Path,
) -> None:
    """Without a usable codeindex (``_codeindex_available=False``), the
    loader hook is skipped wholesale. Even a stale ``.project_map.md``
    sitting on disk from a prior run must not leak into the prompt —
    the gate is correctness, not just optimization."""
    project = tmp_path / "proj"
    project.mkdir()
    data_dir = tmp_path / "ember"
    _write_head_manifest(project, data_dir, COMMIT)
    write_server_supplied_map(
        project=project,
        data_dir=data_dir,
        commit_sha=COMMIT,
        markdown="stale content from old commit\n",
    )

    prompt = _build_session_capture_prompt(project, data_dir, codeindex_available=False)

    assert "## Project Map" not in prompt
    assert "stale content" not in prompt
