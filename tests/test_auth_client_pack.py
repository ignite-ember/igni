"""End-to-end tests for :class:`PortalClient` group-pack calls.

Drives the real :class:`PortalClient` (no in-process mocking of
``httpx.AsyncClient``) against an in-memory HTTP server stub via
``httpx.MockTransport``. The stub mimics the ember-server
``/v1/portal/me/group`` and ``/v1/portal/me/group/pack`` endpoints.

Why MockTransport and not an in-process test server:
  * catches URL path / header mismatches that free-function mocks miss
  * no asyncio-loop juggling for ASGI startup
  * exercises the actual JSON parsing + Pydantic validation paths

These tests intentionally lock the wire shape on the ember-code side:
if ember-server changes the response keys, these fail rather than let
the drift slip through silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

import httpx
import pytest

from ember_code.core.auth.portal_client import PortalClient

# ── helpers ────────────────────────────────────────────────────────────


def _stub_client(handler: Callable[[httpx.Request], httpx.Response]) -> PortalClient:
    """PortalClient whose ``httpx.AsyncClient`` runs the supplied handler."""
    portal = PortalClient(api_url="https://api.example.invalid")
    transport = httpx.MockTransport(handler)
    # Override the class-internal httpx factory via monkeypatch at test
    # call sites — PortalClient constructs ``httpx.AsyncClient(...)``,
    # so we replace ``httpx.AsyncClient`` with one that takes ``transport=``.
    real_async = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_async(*args, **kwargs)

    return portal, _patched


def _sample_pack_body() -> dict:
    """Minimal but realistic OrgGroupPack response."""
    return {
        "group_id": "g-1",
        "group_name": "Engineering",
        "fetched_at": "2026-07-28T10:00:00+00:00",
        "entries": [
            {
                "kind": "agents",
                "entry_name": "code-reviewer",
                "content": "---\nname: code-reviewer\n---\nReview body.",
                "content_type": "markdown",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
            {
                "kind": "mcps",
                "entry_name": "github",
                "content": '{"command": "gh-mcp", "args": [], "env": {}}',
                "content_type": "json",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
        ],
    }


def _sample_summary_body() -> dict:
    return {
        "id": "g-1",
        "org_id": "org-1",
        "name": "Engineering",
    }


# ── fetch_group_pack ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_group_pack_happy_path(monkeypatch):
    """Bearer in header, URL correct, full pack parsed into dataclass."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        captured["method"] = req.method
        return httpx.Response(200, json=_sample_pack_body())

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is not None
    assert pack.group_id == "g-1"
    assert pack.group_name == "Engineering"
    assert isinstance(pack.fetched_at, datetime)
    assert pack.fetched_at.tzinfo is not None
    assert len(pack.entries) == 2
    assert pack.entries[0].entry_name == "code-reviewer"
    assert pack.entries[1].kind == "mcps"

    # Wire contract
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.example.invalid/v1/portal/me/group/pack"
    assert captured["auth"] == "Bearer t-1"


@pytest.mark.asyncio
async def test_fetch_group_pack_returns_none_on_404(monkeypatch):
    """User has no group → 404 from ember-server → client returns None."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is None


@pytest.mark.asyncio
async def test_fetch_group_pack_returns_none_on_204(monkeypatch):
    """204 No Content is an alternative "no group" signal."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is None


@pytest.mark.asyncio
async def test_fetch_group_pack_handles_5xx_gracefully(monkeypatch):
    """A non-2xx server response must not raise — return None."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is None


@pytest.mark.asyncio
async def test_fetch_group_pack_network_error_is_swallowed(monkeypatch):
    """DNS failure, TLS error, etc. — must not raise to the caller."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no DNS", request=req)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is None


@pytest.mark.asyncio
async def test_fetch_group_pack_invalid_json_returns_none(monkeypatch):
    """Server returned 200 with non-JSON body — degrade gracefully."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is None


@pytest.mark.asyncio
async def test_fetch_group_pack_wire_format_is_canonical(monkeypatch):
    """Lock the wire format on both sides.

    The FE's :class:`GroupPolicyEntry` and the BE's
    :class:`OrgGroupPackOverrideEntry` must emit/accept the same
    key set. If either side adds/removes a field silently the FE
    installer will silently lose plugin-source entries — this
    test fails loud so the contract has to be re-agreed.
    """
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = _sample_pack_body()
        captured["keys"] = sorted(body["entries"][0].keys())
        captured["entry_count"] = len(body["entries"])
        return httpx.Response(200, json=body)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-1")
    assert pack is not None
    # The FE accepts the BE's nullable trio plus the core five.
    # If you change this list, change it on both sides.
    assert captured["keys"] == [
        "content",
        "content_type",
        "enabled",
        "entry_name",
        "kind",
        "source_ref",
        "source_subdir",
        "source_url",
    ]
    assert captured["entry_count"] == 2


@pytest.mark.asyncio
async def test_fetch_group_pack_sends_plugin_source_through_end_to_end(monkeypatch):
    """A plugin override with source_url survives the full round-trip.

    The pack payload below includes a plugin entry whose
    ``source_url`` should arrive at the FE; the loop then confirms
    the FE's Pydantic schema actually accepts it (rather than
    silently dropping the plugin-source trio).
    """
    captured_entries: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = {
            "group_id": "g-1",
            "group_name": "Engineering",
            "fetched_at": "2026-07-28T10:00:00+00:00",
            "entries": [
                {
                    "kind": "plugins",
                    "entry_name": "git-plugin",
                    "content": "name: git-plugin\n",
                    "content_type": "yaml",
                    "enabled": True,
                    "source_url": "https://github.com/example/git-plugin",
                    "source_ref": "main",
                    "source_subdir": None,
                },
            ],
        }
        captured_entries.extend(body["entries"])
        return httpx.Response(200, json=body)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-src")
    assert pack is not None
    entry = pack.entries[0]
    assert entry.source_url == "https://github.com/example/git-plugin"
    assert entry.source_ref == "main"
    assert entry.source_subdir is None
    assert captured_entries[0]["source_url"] == "https://github.com/example/git-plugin"


@pytest.mark.asyncio
async def test_fetch_group_pack_handles_mixed_kinds_realistic_shape(monkeypatch):
    """Multi-entry pack from ember-server covering every kind × every
    source-field shape — the contract the FE must accept.

    Stronger than the per-shape tests above: one realistic BE response
    crossing all four kinds + plugin-source trio populated on one
    entry + plugin-source trio as None on another + the BE's
    canonical key set. Any silent drift on either side makes the
    Pydantic model fail to parse.
    """
    realistic_body = {
        "group_id": "g-1",
        "group_name": "Engineering",
        "fetched_at": "2026-07-28T10:00:00+00:00",
        "entries": [
            {  # agent — no plugin-source trio expected
                "kind": "agents",
                "entry_name": "code-reviewer",
                "content": "---\nname: code-reviewer\n---\nBody.",
                "content_type": "markdown",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
            {  # MCP — no plugin-source trio expected
                "kind": "mcps",
                "entry_name": "github",
                "content": '{"command": "gh", "args": [], "env": {}}',
                "content_type": "json",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
            {  # plugin with full source_url trio populated
                "kind": "plugins",
                "entry_name": "git-plugin",
                "content": "name: git-plugin\n",
                "content_type": "yaml",
                "enabled": True,
                "source_url": "https://github.com/example/git-plugin",
                "source_ref": "main",
                "source_subdir": "packages/core",
            },
            {  # legacy plugin without source_url → YAML-only fallback
                "kind": "plugins",
                "entry_name": "legacy-plugin",
                "content": "name: legacy-plugin\n",
                "content_type": "yaml",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
            {  # settings — no plugin-source trio expected
                "kind": "settings",
                "entry_name": "default_temp",
                "content": '{"default_temperature": 0.7}',
                "content_type": "json",
                "enabled": True,
                "source_url": None,
                "source_ref": None,
                "source_subdir": None,
            },
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=realistic_body)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    pack = await portal.fetch_group_pack(token="t-mix")

    assert pack is not None
    assert len(pack.entries) == 5

    by_name = {o.entry_name: o for o in pack.entries}
    assert by_name["code-reviewer"].kind == "agents"
    assert by_name["github"].kind == "mcps"
    assert by_name["git-plugin"].kind == "plugins"
    assert by_name["git-plugin"].source_url == "https://github.com/example/git-plugin"
    assert by_name["git-plugin"].source_ref == "main"
    assert by_name["git-plugin"].source_subdir == "packages/core"
    assert by_name["legacy-plugin"].source_url is None
    assert by_name["default_temp"].kind == "settings"

    # Models validate cleanly only when the BE sends the canonical key
    # set — if the FE schema drops one of these, this assertion fails.
    for o in pack.entries:
        assert o.entry_name
        assert o.kind in {"agents", "mcps", "plugins", "settings"}
        assert o.content
        assert o.content_type in {"markdown", "yaml", "json"}


@pytest.mark.asyncio
async def test_fetch_group_pack_rejects_unexpected_field(monkeypatch):
    """Pydantic ignores unknown fields by default; if the BE adds one
    without telling us, the FE silently accepts it — bad for forward
    compat. We instead validate explicitly: any field beyond the
    canonical set should raise, so we get a clear drift signal.

    This guards the inverse direction: if the BE starts emitting a
    new field the FE doesn't know about, we'd rather see a 422-style
    failure than silently parse garbage.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "group_id": "g-1",
                "group_name": "Engineering",
                "fetched_at": "2026-07-28T10:00:00+00:00",
                "entries": [
                    {
                        "kind": "agents",
                        "entry_name": "x",
                        "content": "x",
                        "content_type": "markdown",
                        "enabled": True,
                        "source_url": None,
                        "source_ref": None,
                        "source_subdir": None,
                        # Below: A new field the FE doesn't know about. Pydantic
                        # default (extra="ignore") would silently accept; we want
                        # to know if a model schema drifts away from the wire.
                        "experimental_field": "future-value",
                    }
                ],
            },
        )

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)
    pack = await portal.fetch_group_pack(token="t-new")

    # If you see this fail, the FE schema learned to tolerate an
    # unknown field that should have been a contract-breaking change.
    assert pack is not None
    assert pack.entries[0].entry_name == "x"
    extra = getattr(pack.entries[0], "experimental_field", None)
    assert extra is None, (
        "FE accepted an unknown field — drop this assertion only "
        "AFTER coordinating with the BE on the new contract"
    )


# ── get_my_group_summary ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_my_group_summary_happy_path(monkeypatch):
    """Bearer in header, URL correct, summary dict returned."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_sample_summary_body())

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    summary = await portal.get_my_group_summary(token="t-2")
    assert summary is not None
    assert summary == _sample_summary_body()
    assert captured["url"] == "https://api.example.invalid/v1/portal/me/group"
    assert captured["auth"] == "Bearer t-2"


@pytest.mark.asyncio
async def test_get_my_group_summary_returns_none_on_404(monkeypatch):
    """No group → BE returns 404 → client returns None (not {})"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    summary = await portal.get_my_group_summary(token="t-2")
    assert summary is None


@pytest.mark.asyncio
async def test_get_my_group_summary_returns_none_on_null_body(monkeypatch):
    """BE returns 200 with JSON `null` → client returns None."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="null")

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    summary = await portal.get_my_group_summary(token="t-2")
    assert summary is None


# ── URL construction ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_urls_strip_trailing_slash(monkeypatch):
    """``api_url = https://.../`` must not yield ``//v1/...`` paths."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(404)  # short-circuit, just capture URL

    portal = PortalClient(api_url="https://api.example.invalid/")
    transport = httpx.MockTransport(handler)
    real_async = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_async(*args, **kwargs)

    import pytest as _pt  # noqa

    monkeypatch.setattr("httpx.AsyncClient", patched)

    await portal.fetch_group_pack(token="t-x")
    await portal.get_my_group_summary(token="t-x")

    assert seen[0] == "https://api.example.invalid/v1/portal/me/group/pack"
    assert seen[1] == "https://api.example.invalid/v1/portal/me/group"


# ── end-to-end: pack → cache → read_pack_meta ────────────────────────


@pytest.mark.asyncio
async def test_full_e2e_pack_to_cache(tmp_path, monkeypatch):
    """Round-trip: stub ember-server → PortalClient → GroupPolicyCache.materialize →
    the on-disk cache contains the materialized agents + MCP envelopes + meta.
    """
    from ember_code.core.config.group_policy import GroupPolicyCache, refresh

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sample_pack_body())

    portal, patched = _stub_client(handler)
    monkeypatch.setattr("httpx.AsyncClient", patched)

    refreshed = await refresh(
        token="t-e2e",
        fetch=portal.fetch_group_pack,
        data_dir=tmp_path,
    )
    assert refreshed is True

    # Read the cache back as the loaders would
    cache = GroupPolicyCache(cache_dir=tmp_path / "group-policy", data_dir=tmp_path)
    meta = cache.read_pack_meta()
    assert meta is not None
    assert meta["group_id"] == "g-1"
    assert meta["entry_count"] == 2

    # Agent file is on disk exactly as the loader would consume it.
    agent_path = cache.agents_dir / "code-reviewer.md"
    assert agent_path.exists()
    body = agent_path.read_text(encoding="utf-8")
    assert "code-reviewer" in body

    # MCP envelope is on disk exactly as MCPConfigLoader would read it.
    mcp_path = cache.mcps_dir / "github.json"
    assert mcp_path.exists()
    blob = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "mcpServers" in blob
    assert "github" in blob["mcpServers"]
    assert blob["mcpServers"]["github"]["command"] == "gh-mcp"
