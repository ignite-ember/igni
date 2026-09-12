"""A group's brand, from the pack to the status push.

The server lets a group carry brand overrides — accent colours, a
wordmark, a logo. They ride the group pack the client already polls,
get written into the cache's metadata, and
leave for the frontend on the status update it already re-renders on.

The backend deliberately does not interpret any of it. What matters
here is only that it survives the trip and that nothing on the path can
take the status bar down with it: the frontend validates every value
against its own allowlist before any of it reaches a stylesheet (see
``clients/web/src/lib/theme.ts``), because a client can be pointed at a
server somebody else runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.config.group_policy import GroupPolicyCache, GroupPolicyPack

THEME = {"accent": "#123456", "brand_name": "Acme"}


def _pack(theme: dict | None) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        overrides=[],
        theme=theme,
    )


class TestThePackCarriesIt:
    def test_a_theme_on_the_wire_is_kept(self):
        assert _pack(THEME).theme == THEME

    def test_a_pack_without_one_has_none(self):
        # Every group that has not set a brand, which is most of them.
        assert _pack(None).theme is None

    def test_an_older_server_that_omits_it_still_parses(self):
        # The field is new; a server that predates it sends no key at
        # all, and that must not fail the whole pack.
        pack = GroupPolicyPack.model_validate(
            {
                "group_id": "g-1",
                "group_name": "Legal",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "overrides": [],
            }
        )
        assert pack.theme is None


class TestTheCacheKeepsIt:
    def test_materialize_writes_the_theme_into_pack_meta(self, tmp_path: Path):
        # ``pack_meta.json`` is what the backend reads on every status
        # poll — if the theme is not written here it never reaches the
        # frontend, however correct the rest of the path is.
        cache = GroupPolicyCache(cache_dir=tmp_path / "cache", data_dir=tmp_path / "data")
        cache.materialize(_pack(THEME))

        meta = json.loads((tmp_path / "cache" / "pack_meta.json").read_text())
        assert meta["theme"] == THEME
        assert cache.read_pack_meta()["theme"] == THEME

    def test_a_group_without_a_brand_writes_none(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path / "cache", data_dir=tmp_path / "data")
        cache.materialize(_pack(None))

        assert cache.read_pack_meta()["theme"] is None
