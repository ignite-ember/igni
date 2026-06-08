"""Tests for ``CodeIndex`` — per-commit chroma + manifest + retention."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ember_code.core.code_index.enums import FileSystemType
from ember_code.core.code_index.index import (
    CodeIndex,
    _branch_heads,
    _decode_bracketed_list,
    _encode_bracketed_list,
    _flatten_item_metadata,
)
from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.paths import commit_chroma_path
from ember_code.core.code_index.schema.items import CodeIndexItem


def _make_item(
    *,
    name: str,
    content: str,
    path: str | None = None,
    domain: list[str] | None = None,
    quality: str | None = None,
    security: str | None = None,
) -> CodeIndexItem:
    return CodeIndexItem(
        item_id=str(uuid.uuid4()),
        name=name,
        content=content,
        type=FileSystemType.FILE,
        kind="code",
        path=path or f"src/{name}",
        repository_id="test-repo",
        file_extension=name.rsplit(".", 1)[-1] if "." in name else None,
        domain=domain or [],
        quality=quality,
        security=security,
    )


@pytest.fixture
async def index(tmp_path):
    idx = CodeIndex(project=tmp_path / "proj_a", data_dir=str(tmp_path / "data"))
    yield idx
    await idx.close()


# -- Manifest -----------------------------------------------------------------


class TestManifest:
    def test_load_missing_file_returns_empty(self, tmp_path):
        m = Manifest(project=tmp_path / "p", data_dir=str(tmp_path / "data"))
        state = m.load()
        assert state.head is None
        assert state.commits == {}

    def test_set_head_creates_commit_entry(self, tmp_path):
        m = Manifest(project=tmp_path / "p", data_dir=str(tmp_path / "data"))
        m.set_head("abc123")
        state = m.load()
        assert state.head == "abc123"
        assert "abc123" in state.commits

    def test_touch_updates_last_used_at(self, tmp_path):
        m = Manifest(project=tmp_path / "p", data_dir=str(tmp_path / "data"))
        m.upsert_commit("abc")
        original = m.load().commits["abc"].last_used_at
        import time

        time.sleep(1.1)
        m.touch("abc")
        assert m.load().commits["abc"].last_used_at != original

    def test_remove_commit_clears_head_when_matching(self, tmp_path):
        m = Manifest(project=tmp_path / "p", data_dir=str(tmp_path / "data"))
        m.set_head("abc")
        m.remove_commit("abc")
        state = m.load()
        assert state.head is None
        assert "abc" not in state.commits

    def test_update_branch_refs(self, tmp_path):
        m = Manifest(project=tmp_path / "p", data_dir=str(tmp_path / "data"))
        m.upsert_commit("abc")
        m.update_branch_refs({"abc": ["main", "develop"]})
        assert m.load().commits["abc"].branch_refs == ["main", "develop"]


# -- Metadata helpers ---------------------------------------------------------


class TestMetadata:
    def test_bracketed_round_trip(self):
        original = ["alpha", "beta", "gamma"]
        encoded = _encode_bracketed_list(original)
        # Bracketed on both sides for $contains exact match.
        assert encoded.startswith("\x1f") and encoded.endswith("\x1f")
        assert _decode_bracketed_list(encoded) == original

    def test_bracketed_drops_empty_strings(self):
        assert _decode_bracketed_list(_encode_bracketed_list(["a", "", "b"])) == ["a", "b"]

    def test_bracketed_empty_round_trip(self):
        assert _encode_bracketed_list([]) == ""
        assert _decode_bracketed_list("") == []

    def test_flatten_promotes_quality_and_lists(self):
        item = _make_item(
            name="x.py",
            content="x",
            quality="poor",
            security="critical",
            domain=["auth", "api"],
        )
        meta = _flatten_item_metadata(item)
        assert meta["quality"] == "poor"
        assert meta["security"] == "critical"
        # Domain is bracketed for exact $contains match.
        assert meta["domain"] == "\x1fauth\x1fapi\x1f"
        # Categoricals not set come through as empty strings.
        assert meta["complexity"] == ""
        # No legacy ``tags`` field on the chroma metadata.
        assert "tags" not in meta


# -- prepare_commit -----------------------------------------------------------


class TestPrepareCommit:
    @pytest.mark.asyncio
    async def test_creates_empty_chroma_when_no_parent(self, index):
        path = await index.prepare_commit("sha_0")
        assert path.exists()
        state = index.manifest.load()
        assert "sha_0" in state.commits

    @pytest.mark.asyncio
    async def test_idempotent_on_existing_commit(self, index):
        await index.prepare_commit("sha_0")
        first_used = index.manifest.load().commits["sha_0"].last_used_at
        import time

        time.sleep(1.1)
        await index.prepare_commit("sha_0")
        assert index.manifest.load().commits["sha_0"].last_used_at != first_used

    @pytest.mark.asyncio
    async def test_copy_from_parent(self, index):
        await index.prepare_commit("parent")
        item = _make_item(name="seed.py", content="seed content")
        await index.add_item("parent", item)

        await index.prepare_commit("child", parent_sha="parent")
        fetched = await index.get_item(item.item_id, commit="child")
        assert fetched is not None
        assert fetched.name == "seed.py"


class TestForgetCommit:
    """``/codeindex resync`` needs to mark a single commit's local
    state as un-indexed so the next sync rebuilds from a snapshot.

    The contract:
    * manifest entry dropped
    * ``has_commit`` reports False afterwards
    * chroma directory STAYS (we don't rmtree under chromadb's live
      process-level client — that triggers SQLITE_READONLY_DBMOVED
      on the next write)
    * idempotent on the unknown case
    """

    @pytest.mark.asyncio
    async def test_forget_drops_manifest_entry_and_reports_not_indexed(self, index):
        path = await index.prepare_commit("doomed")
        assert path.exists()
        assert "doomed" in index.manifest.load().commits
        assert index.has_commit("doomed") is True

        removed = await index.forget_commit("doomed")
        assert removed is True
        assert path.exists(), "chroma dir must stay so the live client doesn't go stale"
        assert "doomed" not in index.manifest.load().commits
        assert index.has_commit("doomed") is False

    @pytest.mark.asyncio
    async def test_forget_unknown_commit_is_noop(self, index):
        removed = await index.forget_commit("never_existed")
        assert removed is False

    @pytest.mark.asyncio
    async def test_forget_empty_sha_is_noop(self, index):
        assert await index.forget_commit("") is False

    @pytest.mark.asyncio
    async def test_has_commit_false_without_manifest_entry(self, index):
        """If the chroma dir exists but the manifest has no entry, the
        commit isn't really indexed — guard against the case where a
        stale dir survives a prior wipe."""
        path = await index.prepare_commit("orphan")
        assert path.exists()
        # Drop the manifest entry behind the index's back.
        index.manifest.remove_commit("orphan")
        assert index.has_commit("orphan") is False


# -- add_item / search / get_item ---------------------------------------------


class TestSearchAndGet:
    @pytest.mark.asyncio
    async def test_search_returns_relevant_first(self, index):
        await index.prepare_commit("head_sha")
        await index.set_head("head_sha")
        await index.add_item(
            "head_sha",
            _make_item(
                name="auth.py",
                content="JWT authentication with HS256 token signing.",
            ),
        )
        await index.add_item(
            "head_sha",
            _make_item(
                name="db.py",
                content="Database connection pooling with retry logic.",
            ),
        )

        results = await index.search(query="JWT signing", limit=5)
        assert results
        assert results[0].name == "auth.py"
        assert results[0].commit == "head_sha"

    @pytest.mark.asyncio
    async def test_search_uses_head_when_no_commit_specified(self, index):
        await index.set_head("a")
        await index.prepare_commit("a")
        await index.add_item("a", _make_item(name="head_only.py", content="head only"))
        results = await index.search(query="head only", limit=3)
        assert results and results[0].name == "head_only.py"

    @pytest.mark.asyncio
    async def test_search_no_head_returns_empty(self, index):
        assert await index.search(query="anything") == []

    @pytest.mark.asyncio
    async def test_search_with_where_filter(self, index):
        """Quality fields land as exact-match chroma metadata — verify a
        ``security="critical"`` filter actually narrows results."""
        await index.prepare_commit("c")
        await index.set_head("c")
        await index.add_item(
            "c",
            _make_item(
                name="risky.py",
                content="raw SQL with user input",
                security="critical",
            ),
        )
        await index.add_item(
            "c",
            _make_item(
                name="safe.py",
                content="parameterized SQL queries",
                security="secure",
            ),
        )

        critical = await index.search(query="SQL", limit=5, where={"security": "critical"})
        assert {r.name for r in critical} == {"risky.py"}

    @pytest.mark.asyncio
    async def test_get_item_round_trip(self, index):
        await index.prepare_commit("c")
        await index.set_head("c")
        item = _make_item(
            name="ref.py",
            content="referenced content",
            domain=["billing"],
            quality="good",
        )
        await index.add_item("c", item)
        fetched = await index.get_item(item.item_id)
        assert fetched is not None
        assert fetched.name == "ref.py"
        assert fetched.domain == ["billing"]
        assert fetched.quality == "good"
        assert fetched.kind == "code"


# -- filter_items -------------------------------------------------------------


class TestFilterItems:
    @pytest.mark.asyncio
    async def test_filter_by_quality(self, index):
        await index.prepare_commit("c")
        await index.set_head("c")
        await index.add_item("c", _make_item(name="poor.py", content="...", quality="poor"))
        await index.add_item("c", _make_item(name="good.py", content="...", quality="good"))

        rows = await index.filter_items(where={"quality": "poor"}, limit=10)
        assert {r.name for r in rows} == {"poor.py"}


# -- remove_item --------------------------------------------------------------


class TestRemove:
    @pytest.mark.asyncio
    async def test_remove_drops_item_and_chunks(self, index):
        await index.set_head("c")
        await index.prepare_commit("c")
        item = _make_item(name="trash.py", content="garbage")
        await index.add_item("c", item)
        assert await index.get_item(item.item_id) is not None
        await index.remove_item("c", item.item_id)
        assert await index.get_item(item.item_id) is None


# -- clean --------------------------------------------------------------------


class TestClean:
    @pytest.mark.asyncio
    async def test_keeps_head(self, index):
        await index.set_head("alpha")
        await index.prepare_commit("alpha")
        dropped = await index.clean(keep_recent_days=0)
        assert "alpha" not in dropped

    @pytest.mark.asyncio
    async def test_drops_stale_non_branch_commits(self, index):
        await index.prepare_commit("stale")
        await index.set_head("head")
        await index.prepare_commit("head")

        state = index.manifest.load()
        state.commits["stale"].last_used_at = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).isoformat(timespec="seconds")
        index.manifest.save(state)

        dropped = await index.clean(keep_recent_days=30)
        assert "stale" in dropped
        assert "head" not in dropped
        assert not commit_chroma_path(index.project, "stale", data_dir=index.data_dir).exists()

    @pytest.mark.asyncio
    async def test_keeps_recent_idle_commits(self, index):
        await index.prepare_commit("recent")
        await index.set_head("head")
        await index.prepare_commit("head")
        dropped = await index.clean(keep_recent_days=30)
        assert "recent" not in dropped


# -- branch resolution --------------------------------------------------------


class TestBranchHeads:
    def test_empty_for_non_git(self, tmp_path):
        assert _branch_heads(tmp_path) == {}

    def test_real_git_returns_branches(self, tmp_path):
        env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_path, check=True)
        (tmp_path / "x.txt").write_text("x")
        subprocess.run(["git", *env_args, "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", *env_args, "commit", "-m", "init"], cwd=tmp_path, check=True)
        subprocess.run(["git", *env_args, "branch", "feature/foo"], cwd=tmp_path, check=True)

        heads = _branch_heads(tmp_path)
        assert set(heads.keys()) == {"main", "feature/foo"}
        assert len(set(heads.values())) == 1
