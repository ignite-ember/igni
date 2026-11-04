"""A group policy no longer installs extensions.

It could: a pack entry of ``kind: "extensions"`` named a repository,
the cache cloned it, and the next boot installed it. That path is
gone. An extension reaches a machine from its service, through igni
server — signed with the key a super admin approved
(``docs/service-identity.md`` §8) — and a policy is somebody else
deciding what runs here with nothing attesting to the code.

What is checked: an ``extensions`` entry stages nothing, clones
nothing, says why in the log, and does not cost the group the rest of
its pack.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)


def pack_with(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Engineering",
        version="1",
        fetched_at="2026-10-24T00:00:00+00:00",
        entries=list(entries),
    )


def extension_entry(name: str, url: str) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind="extensions",
        entry_name=name,
        content="",
        content_type="json",
        source_url=url,
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(cache_dir=tmp_path / "group-policy")


def test_an_extension_entry_stages_nothing(cache, tmp_path, monkeypatch, caplog):
    cloned: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            cloned.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    with caplog.at_level(logging.WARNING):
        cache.materialize(pack_with(extension_entry("scanner", f"file://{tmp_path}/repo")))

    assert not (tmp_path / "group-policy" / "extensions").exists()
    assert cloned == [], "a policy must not fetch an extension at all"
    assert "no longer installs extensions" in caplog.text


def test_the_rest_of_the_pack_still_applies(cache):
    agent = GroupPolicyEntry(
        kind="agents",
        entry_name="reviewer",
        content="---\nname: reviewer\ndescription: x\n---\n\nReview.\n",
        content_type="markdown",
    )
    cache.materialize(pack_with(extension_entry("scanner", "file:///nowhere"), agent))
    assert (cache.agents_dir / "reviewer.md").is_file()


def test_the_pack_no_longer_has_extension_accessors():
    """The two accessors that fed the staging path are gone with it."""
    pack = pack_with(extension_entry("scanner", "file:///nowhere"))
    assert not hasattr(pack, "extension_entries")
    assert not hasattr(pack, "extension_disabled_names")
    assert not hasattr(GroupPolicyCache, "extensions_dir")
