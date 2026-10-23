"""Retention has to reclaim the bytes that actually grow.

Since the Neo4j cutover each indexed commit gets a complete Neo4j store at
``~/.igni/neo4j/state/<project>-<sha>/``. ``clean()`` evicted the manifest entry
and — on the shipped path, runtime attached and no shared client — ran a bare
``pass`` where the drop belonged; ``sweep_stale_dirs`` then looked only for
legacy ``code_index/<sha>.chroma`` directories, which nothing has written since
the cutover. Net effect: one whole Neo4j data directory per indexed commit, no
bound, nothing that removes it. A developer switching branches pays for it in
disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.data_paths import neo4j_runtime_dir, neo4j_state_root


def _index(tmp_path: Path, *, runtime=None) -> CodeIndex:
    project = tmp_path / "repo"
    project.mkdir(exist_ok=True)
    return CodeIndex(project=project, data_dir=tmp_path / "data", runtime=runtime)


def _state_dir_with_bytes(index: CodeIndex, sha: str) -> Path:
    path = neo4j_runtime_dir(index.project, sha, data_dir=index.data_dir)
    (path / "data" / "databases" / "neo4j").mkdir(parents=True, exist_ok=True)
    (path / "data" / "databases" / "neo4j" / "neostore").write_bytes(b"x" * 1024)
    return path


class TestTheSweepReclaimsTheNeo4jStores:
    def test_an_untracked_commit_store_is_removed(self, tmp_path: Path):
        index = _index(tmp_path)
        index.manifest.upsert_commit("keepsha")
        kept = _state_dir_with_bytes(index, "keepsha")
        orphan = _state_dir_with_bytes(index, "goneshaaa")

        removed = index.sweep_stale_dirs()

        assert "goneshaaa" in removed
        assert not orphan.exists(), "the per-commit Neo4j store is still on disk"
        assert kept.exists(), "a tracked commit's store was deleted"

    def test_the_knowledge_store_is_never_swept(self, tmp_path: Path):
        """``<project>-knowledge`` shares the state directory and is per-project,
        so it is never in the commit manifest — reading it as an untracked commit
        would delete the project's knowledge graph."""
        index = _index(tmp_path)
        knowledge = neo4j_state_root(data_dir=index.data_dir) / f"{index.project_id}-knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "data").mkdir()

        index.sweep_stale_dirs()

        assert knowledge.exists()

    def test_another_project_is_not_touched(self, tmp_path: Path):
        index = _index(tmp_path)
        other = neo4j_state_root(data_dir=index.data_dir) / "0123456789abcdef-somesha"
        other.mkdir(parents=True)

        index.sweep_stale_dirs()

        assert other.exists()

    def test_a_store_with_a_live_process_is_left_alone(self, tmp_path: Path):
        """Another backend may still be attached by refcount. Deleting the store
        underneath a running Neo4j corrupts it; the next sweep gets it."""
        index = _index(tmp_path)
        live = _state_dir_with_bytes(index, "livesha")
        (live / "runtime.json").write_text(json.dumps({"pid": os.getpid()}))

        removed = index.sweep_stale_dirs()

        assert "livesha" not in removed
        assert live.exists()

    def test_a_store_whose_process_is_gone_is_removed(self, tmp_path: Path):
        index = _index(tmp_path)
        dead = _state_dir_with_bytes(index, "deadsha")
        # A pid that cannot be running: os.kill raises ProcessLookupError.
        (dead / "runtime.json").write_text(json.dumps({"pid": 2**31 - 1}))

        removed = index.sweep_stale_dirs()

        assert "deadsha" in removed
        assert not dead.exists()

    def test_it_is_a_no_op_when_nothing_has_been_indexed(self, tmp_path: Path):
        assert _index(tmp_path).sweep_stale_dirs() == []


class TestEvictionStopsTheCommitProcess:
    @pytest.mark.asyncio
    async def test_clean_asks_the_runtime_to_stop_the_evicted_commit(self, tmp_path: Path):
        """The production wiring is ``runtime=`` with no shared client — the
        branch that used to be a bare ``pass``."""
        runtime = AsyncMock()
        index = _index(tmp_path, runtime=runtime)
        stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        index.manifest.upsert_commit("oldsha")
        state = index.manifest.load()
        state.commits["oldsha"].last_used_at = stale
        index.manifest.save(state)

        dropped = await index.clean(keep_recent_days=30)

        assert dropped == ["oldsha"]
        runtime.stop_for_commit.assert_awaited_once_with(index.project_id, "oldsha")
        assert "oldsha" not in index.manifest.load().commits

    @pytest.mark.asyncio
    async def test_eviction_then_sweep_actually_frees_the_disk(self, tmp_path: Path):
        """The two halves together are the whole of retention on this path."""
        runtime = AsyncMock()
        index = _index(tmp_path, runtime=runtime)
        index.manifest.upsert_commit("oldsha")
        state = index.manifest.load()
        state.commits["oldsha"].last_used_at = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat()
        index.manifest.save(state)
        store = _state_dir_with_bytes(index, "oldsha")

        await index.clean(keep_recent_days=30)
        index.sweep_stale_dirs()

        assert not store.exists(), "retention ran end to end and reclaimed nothing"
