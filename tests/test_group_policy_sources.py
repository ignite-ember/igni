"""Tests for the plugin-source trio on group policy overrides.

Covers two surfaces:

  * :class:`GroupPolicyOverrideEntry` — the wire shape. Pydantic
    validation rejects half-filled source specs (ref/subdir without
    URL) so the BE catches the mistake before it ever reaches the
    installer.

  * :class:`GroupPolicyCache._materialize_plugin` — runtime behavior.
    With a stub installer, plugin entries with a source route through
    ``installer.install(url, ref=, subdir=)``. Entries without a
    source still write YAML to the legacy path. Failures are logged
    and swallowed so one bad plugin can't take down the rest of
    the pack.

The plugin loader's new ``group-policy-ember`` root is covered in
``test_plugins_loader_root_priority.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ember_code.core.config import group_policy as gp_mod
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyOverrideEntry,
    GroupPolicyPack,
)
from ember_code.core.plugins.loader import PluginLoader

# ── Pydantic surface ──────────────────────────────────────────


class TestGroupPolicyOverrideEntrySourceFields:
    """The cross-field validator on GroupPolicyOverrideEntry."""

    def test_empty_source_fields_pass(self):
        entry = GroupPolicyOverrideEntry(
            kind="plugins",
            entry_name="x",
            content="name: x",
            content_type="yaml",
        )
        assert entry.source_url is None
        assert entry.source_ref is None
        assert entry.source_subdir is None

    def test_full_source_triplet_passes(self):
        entry = GroupPolicyOverrideEntry(
            kind="plugins",
            entry_name="x",
            content="name: x",
            content_type="yaml",
            source_url="https://github.com/owner/repo",
            source_ref="main",
            source_subdir="plugins/x",
        )
        assert entry.source_url == "https://github.com/owner/repo"
        assert entry.source_ref == "main"
        assert entry.source_subdir == "plugins/x"

    def test_url_only_passes(self):
        """A bare URL is the minimum: no ref or subdir required."""
        entry = GroupPolicyOverrideEntry(
            kind="plugins",
            entry_name="x",
            content="",
            content_type="yaml",
            source_url="https://example.com/repo",
        )
        assert entry.source_url == "https://example.com/repo"
        assert entry.source_ref is None
        assert entry.source_subdir is None

    def test_ref_without_url_is_rejected(self):
        """ref + no url → cross-field validator raises."""
        with pytest.raises(ValidationError) as exc_info:
            GroupPolicyOverrideEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_ref="main",
            )
        assert "source_url" in str(exc_info.value)

    def test_subdir_without_url_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            GroupPolicyOverrideEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_subdir="plugins/x",
            )
        assert "source_url" in str(exc_info.value)

    def test_ref_and_subdir_without_url_is_rejected(self):
        with pytest.raises(ValidationError):
            GroupPolicyOverrideEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_ref="main",
                source_subdir="plugins/x",
            )

    def test_empty_string_url_with_ref_is_still_rejected(self):
        """Strict behavior: an empty string is still a value, not
        missing. Stops the FE from sneaking a ref past validation
        by setting URL = "".
        """
        with pytest.raises(ValidationError):
            GroupPolicyOverrideEntry(
                kind="plugins",
                entry_name="x",
                content="",
                content_type="yaml",
                source_url="",
                source_ref="main",
            )


# ── Cache materialization ──────────────────────────────────────


class _StubInstaller:
    """Captures install() calls for assertion."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def install(
        self,
        url: str,
        *,
        ref: str | None = None,
        subdir: str | None = None,
    ):
        self.calls.append({"url": url, "ref": ref, "subdir": subdir})
        if self._raises is not None:
            raise self._raises


def _pack_with(overrides: list[GroupPolicyOverrideEntry]) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Test",
        fetched_at=datetime.now(timezone.utc),
        overrides=overrides,
    )


class TestGroupPolicyCacheMaterializePlugin:
    """Source URL routes to installer; no source routes to YAML fallback."""

    def test_source_url_calls_installer_with_args(self, tmp_path: Path):
        cache_dir = tmp_path / "gp"
        installer = _StubInstaller()
        cache = GroupPolicyCache(
            cache_dir=cache_dir,
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="remote-plugin",
                    content="",
                    content_type="yaml",
                    source_url="https://github.com/owner/repo",
                    source_ref="main",
                    source_subdir="plugins/agentic-eval",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == [
            {
                "url": "https://github.com/owner/repo",
                "ref": "main",
                "subdir": "plugins/agentic-eval",
            }
        ]
        # No legacy YAML written for source-equipped plugins.
        assert not (cache_dir / "plugins" / "remote-plugin.yaml").exists()

    def test_url_only_invokes_installer_without_ref_or_subdir(self, tmp_path: Path):
        installer = _StubInstaller()
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="minimal",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == [
            {"url": "https://example.com/r.git", "ref": None, "subdir": None}
        ]

    def test_no_source_writes_legacy_yaml_fallback(self, tmp_path: Path):
        installer = _StubInstaller()
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="legacy",
                    content="name: legacy\n",
                    content_type="yaml",
                ),
            ]
        )

        cache.materialize(pack)

        # Installer is NOT called for entries without a source.
        assert installer.calls == []
        legacy = (tmp_path / "gp" / "plugins" / "legacy.yaml").read_text()
        assert legacy == "name: legacy\n"

    def test_disabled_source_url_skips_install(self, tmp_path: Path):
        """Disabled overrides are filtered out before install — an admin
        can temporarily turn off a plugin without un-installing it on
        disk.
        """
        installer = _StubInstaller()
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="off",
                    content="",
                    content_type="yaml",
                    enabled=False,
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        cache.materialize(pack)
        assert installer.calls == []

    def test_installer_failure_is_swallowed(self, tmp_path: Path):
        """One bad plugin must not abort the rest of the pack."""
        installer = _StubInstaller(
            raises=Exception("network unreachable"),
        )
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="bad",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/bad.git",
                ),
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="good",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/good.git",
                ),
            ]
        )

        # Should not raise — both plugins attempted.
        cache.materialize(pack)
        assert len(installer.calls) == 2

    def test_no_installer_configured_logs_warning_no_crash(self, tmp_path: Path):
        """A cache built without an installer for a source-URL row
        shouldn't crash — the row will simply be skipped. Real
        callers wire an installer; this catches dev misuse.

        We patch the module logger directly instead of using
        ``caplog``. Other tests in this repo reconfigure logging
        at runtime, which makes ``caplog`` unreliable when the
        full suite runs (mirrors the rationale in
        ``test_codeindex_filters.py``).
        """
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=None,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="plugins",
                    entry_name="stranded",
                    content="",
                    content_type="yaml",
                    source_url="https://example.com/r.git",
                ),
            ]
        )

        with patch.object(gp_mod.logger, "warning") as mock_warning:
            cache.materialize(pack)

        assert mock_warning.called, "expected a warning about missing installer"
        messages = [
            (args[0] % args[1:]) if len(args) > 1 else args[0]
            for args, _ in mock_warning.call_args_list
        ]
        joined = " ".join(messages).lower()
        assert "no installer" in joined
        assert "stranded" in joined

    def test_other_kinds_unaffected(self, tmp_path: Path):
        """Agents/MCPs/settings still write to their normal paths even
        when ``installer`` is wired. The cache only forks on
        kind=plugins.
        """
        installer = _StubInstaller()
        cache = GroupPolicyCache(
            cache_dir=tmp_path / "gp",
            installer=installer,
            data_dir=tmp_path,
        )

        pack = _pack_with(
            [
                GroupPolicyOverrideEntry(
                    kind="agents",
                    entry_name="reviewer",
                    content="---\nname: reviewer\n---\n# body",
                    content_type="markdown",
                ),
                GroupPolicyOverrideEntry(
                    kind="mcps",
                    entry_name="gh",
                    content='{"mcpServers": {}}',
                    content_type="json",
                ),
            ]
        )

        cache.materialize(pack)

        assert installer.calls == []  # agents/mcps don't install
        assert (tmp_path / "gp" / "agents" / "reviewer.md").exists()
        assert (tmp_path / "gp" / "mcps" / "gh.json").exists()


# ── Plugin loader: new root ────────────────────────────────────


class TestPluginLoaderGroupPolicyRoot:
    """The new ``group-policy-ember`` root at priority 5."""

    @staticmethod
    def _capture_roots(tmp_path: Path) -> dict[str, tuple[Path, int]]:
        """Run load_all and return the (path, priority) tuple per root."""
        loader = PluginLoader(data_dir=tmp_path)
        captured: list[tuple[str, Path, int]] = []
        loader._load_root = lambda k, rp, prio: captured.append((k, rp, prio))
        loader.load_all(tmp_path)
        return {k: (rp, prio) for k, rp, prio in captured}

    def test_root_registered_with_data_dir_relative_path(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        assert "group-policy-ember" in roots
        path, priority = roots["group-policy-ember"]
        assert path == tmp_path / "group-policy" / "plugins"
        assert priority == 5

    def test_priority_outranks_user_and_project_tiers(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        prios = {k: v[1] for k, v in roots.items()}

        # Outrank user + project
        assert prios["group-policy-ember"] > prios["user-ember"]
        assert prios["group-policy-ember"] > prios["project-ember"]
        assert prios["group-policy-ember"] > prios["project-claude"]

    def test_priority_loses_to_managed_tiers(self, tmp_path: Path):
        roots = self._capture_roots(tmp_path)
        prios = {k: v[1] for k, v in roots.items()}

        # Submit to the OS-managed tiers
        assert prios["group-policy-ember"] < prios["managed-claude"]
        assert prios["group-policy-ember"] < prios["managed-ember"]

    def test_loader_does_not_flag_group_policy_as_managed(self, tmp_path: Path):
        """Group-policy installs aren't OS-managed — users can still
        disable them via the existing ``plugin_disabled_names`` flow.
        """
        # Lay down a stub plugin under the new root.
        plugin_root = tmp_path / "group-policy" / "plugins" / "test-plugin"
        plugin_root.mkdir(parents=True)
        manifest_dir = plugin_root / ".claude-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "test-plugin",
                    "version": "1.0.0",
                    "description": "Test plugin",
                }
            )
        )

        loader = PluginLoader(data_dir=tmp_path)
        loader.load_all(tmp_path)

        plugin = loader.get("test-plugin")
        assert plugin is not None
        # Root kind, not priority, drives is_managed — and the
        # new tier isn't in the managed-claude/managed-ember set.
        assert plugin.is_managed is False


# ---------------------------------------------------------------------------
# Cache hydration (refresh_if_stale + module-level refresh helper)
# ---------------------------------------------------------------------------


def _make_pack(*, fetched_at: datetime) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-test",
        group_name="Engineering",
        fetched_at=fetched_at,
        overrides=[
            GroupPolicyOverrideEntry(
                kind="agents",
                entry_name="code-reviewer",
                content="---\nname: code-reviewer\n---\nA reviewer.",
                content_type="markdown",
                enabled=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_refresh_if_stale_skips_when_cache_is_fresh(tmp_path: Path):
    """A 60-second-old cache does NOT trigger a fetch."""
    cache = GroupPolicyCache(cache_dir=tmp_path)
    cache.materialize(_make_pack(fetched_at=datetime.now(timezone.utc).replace(microsecond=0)))

    fetch_called = False

    async def _fetch(token: str):
        nonlocal fetch_called
        fetch_called = True
        return None

    refreshed = await cache.refresh_if_stale(token="t-1", fetch=_fetch)
    assert refreshed is False
    assert fetch_called is False


@pytest.mark.asyncio
async def test_refresh_if_stale_fetches_when_meta_missing(tmp_path: Path):
    """Empty cache directory → fetch + materialize."""
    cache = GroupPolicyCache(cache_dir=tmp_path)
    pack = _make_pack(fetched_at=datetime.now(timezone.utc))

    async def _fetch(token: str):
        assert token == "t-2"
        return pack

    refreshed = await cache.refresh_if_stale(token="t-2", fetch=_fetch)
    assert refreshed is True
    # Cache now populated
    meta = cache.read_pack_meta()
    assert meta is not None
    assert meta["group_id"] == "g-test"
    # And the agent file is on disk
    assert (cache.agents_dir / "code-reviewer.md").exists()


@pytest.mark.asyncio
async def test_refresh_if_stale_fetches_when_cache_is_stale(tmp_path: Path):
    """A 600-second-old cache (> 5 min TTL) DOES trigger a fetch."""
    cache = GroupPolicyCache(cache_dir=tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    cache.materialize(_make_pack(fetched_at=old))

    new_pack = _make_pack(fetched_at=datetime.now(timezone.utc).replace(microsecond=0))

    async def _fetch(token: str):
        return new_pack

    refreshed = await cache.refresh_if_stale(token="t-3", fetch=_fetch)
    assert refreshed is True
    # Pack meta must be re-stamped with the fresh fetched_at
    meta = cache.read_pack_meta()
    assert meta is not None
    # Compare as parsed datetimes to avoid second-level granularity
    fresh = datetime.fromisoformat(meta["fetched_at"])
    assert fresh > old


@pytest.mark.asyncio
async def test_refresh_if_stale_returns_false_when_fetch_returns_none(
    tmp_path: Path,
):
    """Network/server failure → no disk write, no crash."""
    cache = GroupPolicyCache(cache_dir=tmp_path)

    async def _fetch(token: str):
        return None

    refreshed = await cache.refresh_if_stale(token="t-4", fetch=_fetch)
    # Pack was None — disk untouched, return False ("no update").
    assert refreshed is False
    assert cache.read_pack_meta() is None


@pytest.mark.asyncio
async def test_refresh_if_stale_treats_empty_token_as_noop(tmp_path: Path):
    """An empty bearer must not call fetch."""
    cache = GroupPolicyCache(cache_dir=tmp_path)

    async def _fetch(token: str):
        raise AssertionError("fetch should not be called")

    refreshed = await cache.refresh_if_stale(token="", fetch=_fetch)
    assert refreshed is False


@pytest.mark.asyncio
async def test_module_level_refresh_helper(tmp_path: Path):
    """The top-level ``refresh`` constructs a one-shot cache."""
    from ember_code.core.config.group_policy import refresh

    pack = _make_pack(fetched_at=datetime.now(timezone.utc))

    async def _fetch(token: str):
        return pack

    refreshed = await refresh(
        token="t-5",
        fetch=_fetch,
        data_dir=tmp_path,
    )
    assert refreshed is True
    # Default cache_dir derives from data_dir/group-policy
    on_disk = tmp_path / "group-policy" / "agents" / "code-reviewer.md"
    assert on_disk.exists()
