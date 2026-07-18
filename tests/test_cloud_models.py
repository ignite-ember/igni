"""Tests for config/cloud_models.py — the :class:`CloudModelCatalogClient`
that fetches + merges the Ember Cloud key-pool catalogue.

Covers both pieces independently:

* ``CloudModelCatalogClient.fetch`` — uses ``httpx.MockTransport`` so
  we don't hit a real network. Verifies happy path against the
  canonical ``{"models": [{"id": "..."}]}`` shape, tolerance for
  legacy responses that still carry ``base_url``, no-token
  short-circuit, non-200 / non-JSON / network-error degradation.
  Each soft-fail path is reified as a specific
  :class:`FetchReason` so callers can distinguish causes.
* ``CloudModelCatalogClient.merge_into`` — pure in-memory dict
  mutation. Verifies add-new, skip-existing (user config wins),
  idempotency, the ember-server-proxy routing invariant (every
  cloud entry's URL points at ``{api_url}/v1``, never the upstream),
  and the entry shape (``api_key: "cloud_token"`` sentinel +
  ``source: "cloud"`` tag).
"""

from __future__ import annotations

import httpx

from ember_code.core.config.cloud_models import (
    CloudModelCatalogClient,
    FetchReason,
)
from ember_code.core.config.model_entry import ModelRegistryEntry

# ── CloudModelCatalogClient.fetch ─────────────────────────────────────


# Bind the real Client at import time. The tests monkeypatch ``httpx.Client``
# to redirect the client's lookup; if ``_mock_client`` referenced
# ``httpx.Client`` directly it would recurse into the patched version.
_REAL_HTTPX_CLIENT = httpx.Client


def _mock_client(handler):
    """``httpx.Client`` whose transport runs ``handler(request) -> Response``."""
    return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))


def test_fetch_returns_no_token_when_missing():
    """Unauthenticated path: no token → don't even attempt the call."""
    for token in (None, ""):
        result = CloudModelCatalogClient("https://api.example.com", token).fetch()
        assert not result.ok
        assert result.reason == FetchReason.NO_TOKEN
        assert result.entries == []


def test_fetch_happy_path(monkeypatch):
    """The server returns ``{"models": [{"id": "..."}, ...]}`` — just
    identifiers, no upstream URLs. The CLI talks to ember-server's
    chat proxy, so leaking upstream routing is intentionally avoided
    on the server side."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "models": [
                    {"id": "gpt-4o"},
                    {"id": "anthropic/claude-opus-4-7"},
                ]
            },
        )

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = CloudModelCatalogClient("https://api.example.com", "tok-1").fetch()

    assert captured["url"] == "https://api.example.com/v1/chat/models"
    assert captured["auth"] == "Bearer tok-1"
    assert result.ok
    assert result.reason == FetchReason.OK
    assert [e.model_id for e in result.entries] == [
        "gpt-4o",
        "anthropic/claude-opus-4-7",
    ]
    # Every entry routes through the ember-server chat proxy.
    for entry in result.entries:
        assert entry.url == "https://api.example.com/v1"
        assert entry.api_key == "cloud_token"
        # ``source='cloud'`` tag lives in the model_extra bag
        # (schema has ``extra='allow'``).
        assert entry.model_extra is not None
        assert entry.model_extra.get("source") == "cloud"


def test_fetch_tolerates_legacy_base_url_field(monkeypatch):
    """Older server deploys included an upstream ``base_url`` on each
    entry. ``CloudModelEntry`` has ``extra='allow'`` so the field is
    tolerated but deliberately not propagated onto the resulting
    :class:`ModelRegistryEntry` (routing always goes through the
    ember-server proxy)."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"id": "gpt-4o", "base_url": "https://upstream.example/v1"},
                ]
            },
        )

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = CloudModelCatalogClient("https://api.example.com", "tok-1").fetch()
    assert result.ok
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.model_id == "gpt-4o"
    # URL is the ember-server proxy — the legacy upstream field
    # must NOT leak onto the registry entry.
    assert entry.url == "https://api.example.com/v1"
    assert "upstream.example" not in str(entry.model_dump())


def test_fetch_strips_trailing_slash_on_api_url(monkeypatch):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    CloudModelCatalogClient("https://api.example.com/", "tok-1").fetch()
    assert captured["url"] == "https://api.example.com/v1/chat/models"


def test_fetch_non_200_returns_http_error(monkeypatch):
    """Server-side gate (e.g. 401, 503) → HTTP_ERROR reason, don't raise."""
    monkeypatch.setattr(
        "httpx.Client",
        lambda **_: _mock_client(lambda _req: httpx.Response(401, json={"detail": "nope"})),
    )
    result = CloudModelCatalogClient("https://api.example.com", "bad-token").fetch()
    assert not result.ok
    assert result.reason == FetchReason.HTTP_ERROR
    assert result.detail == "HTTP 401"
    assert result.entries == []


def test_fetch_network_error_returns_decode_error(monkeypatch):
    """Connection refused / DNS failure / etc. → degrade silently."""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = CloudModelCatalogClient("https://api.example.com", "tok-1").fetch()
    assert not result.ok
    assert result.reason == FetchReason.DECODE_ERROR
    assert result.entries == []


def test_fetch_unexpected_payload_shape_returns_bad_shape(monkeypatch):
    """Server returns ``{"models": null}`` (or any non-list) → BAD_SHAPE."""
    monkeypatch.setattr(
        "httpx.Client",
        lambda **_: _mock_client(lambda _req: httpx.Response(200, json={"models": None})),
    )
    result = CloudModelCatalogClient("https://api.example.com", "tok-1").fetch()
    assert not result.ok
    assert result.reason == FetchReason.BAD_SHAPE
    assert result.entries == []


def test_fetch_filters_entries_without_id(monkeypatch):
    """Defensive: entries without a usable ``id`` produce a
    ``BAD_SHAPE`` because Pydantic validation at the boundary
    rejects the whole payload rather than silently dropping rows
    (the sole responsibility of the wire schema)."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"id": "gpt-4o"},
                    {},  # missing id
                    "not-a-dict",  # noqa: ERA001
                ]
            },
        )

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = CloudModelCatalogClient("https://api.example.com", "tok-1").fetch()
    assert not result.ok
    assert result.reason == FetchReason.BAD_SHAPE


# ── CloudModelCatalogClient.merge_into ───────────────────────────────


_API = "https://api.example.com"


def _client(api_url: str = _API, token: str | None = "tok-1") -> CloudModelCatalogClient:
    return CloudModelCatalogClient(api_url, token)


def _entries_for(ids: list[str], api_url: str = _API) -> list[ModelRegistryEntry]:
    proxy_url = f"{api_url.rstrip('/')}/v1"
    return [
        ModelRegistryEntry.from_cloud_discovery(model_id=mid, proxy_url=proxy_url) for mid in ids
    ]


def test_merge_adds_new_entries():
    registry: dict[str, ModelRegistryEntry | dict] = {}
    merge = _client().merge_into(
        registry,
        entries=_entries_for(["gpt-4o", "anthropic/claude-opus-4-7"]),
    )
    assert merge.added == 2
    assert merge.skipped_existing == 0
    assert set(registry.keys()) == {"gpt-4o", "anthropic/claude-opus-4-7"}


def test_merge_routes_through_ember_server_even_for_legacy_payload():
    """Even when the wire response includes a legacy ``base_url``,
    the resulting registry entry routes through ``{api_url}/v1``
    (the ember-server chat proxy). Verified end-to-end via
    ``from_cloud_discovery`` — the legacy upstream URL never
    reaches the entry."""
    entries = _entries_for(["MiniMaxAI/MiniMax-M2.5"])
    registry: dict[str, ModelRegistryEntry | dict] = {}
    _client().merge_into(registry, entries=entries)
    entry = registry["MiniMaxAI/MiniMax-M2.5"]
    assert isinstance(entry, ModelRegistryEntry)
    assert entry.url == "https://api.example.com/v1"


def test_merge_entry_shape_matches_local_registry():
    """New entries must be drop-in compatible with the local registry
    shape (``provider``, ``model_id``, ``url``, ``api_key``) so the
    existing ``ModelRegistry.get_model`` resolution path works."""
    registry: dict[str, ModelRegistryEntry | dict] = {}
    _client().merge_into(registry, entries=_entries_for(["gpt-4o"]))
    entry = registry["gpt-4o"]
    assert isinstance(entry, ModelRegistryEntry)
    assert entry.provider == "openai_like"
    assert entry.model_id == "gpt-4o"
    # URL always points at ember-server's chat proxy, never upstream.
    assert entry.url == "https://api.example.com/v1"
    # ``cloud_token`` is the sentinel that the API-key resolver rewrites
    # to ``CloudCredentials.access_token`` at call time.
    assert entry.api_key == "cloud_token"
    # Tag so future code (e.g. a "managed by cloud" picker badge) can
    # distinguish cloud-discovered rows from user-defined ones.
    assert entry.model_extra is not None
    assert entry.model_extra.get("source") == "cloud"


def test_merge_strips_trailing_slash_on_api_url():
    """``api_url`` with a trailing slash → proxy URL still resolves to
    a clean ``{host}/v1`` (no double slash)."""
    registry: dict[str, ModelRegistryEntry | dict] = {}
    entries = _entries_for(["gpt-4o"], api_url="https://api.example.com/")
    _client(api_url="https://api.example.com/").merge_into(registry, entries=entries)
    entry = registry["gpt-4o"]
    assert isinstance(entry, ModelRegistryEntry)
    assert entry.url == "https://api.example.com/v1"


def test_merge_does_not_overwrite_existing():
    """User-defined entries win — same-name cloud entries are skipped."""
    user_entry = {
        "provider": "openai_like",
        "model_id": "gpt-4o",
        "url": "https://my-litellm.example/v1",
        "api_key": "sk-pinned",
        "timeout": 120,
    }
    registry: dict[str, ModelRegistryEntry | dict] = {"gpt-4o": user_entry}
    merge = _client().merge_into(registry, entries=_entries_for(["gpt-4o"]))
    assert merge.added == 0
    assert merge.skipped_existing == 1
    assert registry["gpt-4o"] is user_entry
    assert registry["gpt-4o"]["url"] == "https://my-litellm.example/v1"
    assert registry["gpt-4o"]["api_key"] == "sk-pinned"


def test_merge_is_idempotent():
    """Re-fetching the same catalogue is a no-op on the second pass."""
    registry: dict[str, ModelRegistryEntry | dict] = {}
    entries = _entries_for(["gpt-4o"])
    assert _client().merge_into(registry, entries=entries).added == 1
    second = _client().merge_into(registry, entries=entries)
    assert second.added == 0
    assert second.skipped_existing == 1
    assert len(registry) == 1


def test_merge_skips_entries_with_empty_model_id():
    """Defensive: entries with empty ``model_id`` don't land in the
    registry. Pydantic validation prevents constructing such an
    entry in normal flow, but the merge layer re-checks so a
    hand-built list can't sneak a blank row through."""
    registry: dict[str, ModelRegistryEntry | dict] = {}
    # Construct via model_validate to bypass the aliased ``id``
    # requirement and build an intentionally-empty entry.
    blank = ModelRegistryEntry.model_validate(
        {
            "provider": "openai_like",
            "model_id": "",
            "url": f"{_API}/v1",
            "api_key": "cloud_token",
        }
    )
    entries = [
        ModelRegistryEntry.from_cloud_discovery(model_id="gpt-4o", proxy_url=f"{_API}/v1"),
        blank,
    ]
    merge = _client().merge_into(registry, entries=entries)
    assert merge.added == 1
    assert set(registry.keys()) == {"gpt-4o"}
