"""Tests for config/cloud_models.py — fetch + merge of the Ember Cloud
key-pool catalogue.

Covers both pieces independently:

* ``fetch_cloud_models`` — uses ``httpx.MockTransport`` so we don't
  hit a real network. Verifies happy path against the canonical
  ``{"models": [{"id": "..."}]}`` shape, tolerance for legacy
  responses that still carry ``base_url``, no-token short-circuit,
  non-200 / non-JSON / network-error degradation.
* ``merge_into_registry`` — pure in-memory dict mutation. Verifies
  add-new, skip-existing (user config wins), idempotency, the
  ember-server-proxy routing invariant (every cloud entry's URL
  points at ``{api_url}/v1``, never the upstream), and the entry
  shape (``api_key: "cloud_token"`` sentinel + ``source: "cloud"``
  tag).
"""

from __future__ import annotations

import httpx

from ember_code.core.config.cloud_models import fetch_cloud_models, merge_into_registry

# ── fetch_cloud_models ───────────────────────────────────────────────


# Bind the real Client at import time. The tests monkeypatch ``httpx.Client``
# to redirect ``fetch_cloud_models``'s lookup; if ``_mock_client`` referenced
# ``httpx.Client`` directly it would recurse into the patched version.
_REAL_HTTPX_CLIENT = httpx.Client


def _mock_client(handler):
    """``httpx.Client`` whose transport runs ``handler(request) -> Response``."""
    return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))


def test_fetch_returns_empty_when_no_token():
    """Unauthenticated path: no token → don't even attempt the call."""
    assert fetch_cloud_models("https://api.example.com", None) == []
    assert fetch_cloud_models("https://api.example.com", "") == []


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
    result = fetch_cloud_models("https://api.example.com", "tok-1")

    assert captured["url"] == "https://api.example.com/v1/chat/models"
    assert captured["auth"] == "Bearer tok-1"
    assert result == [
        {"id": "gpt-4o"},
        {"id": "anthropic/claude-opus-4-7"},
    ]


def test_fetch_tolerates_legacy_base_url_field(monkeypatch):
    """Older server deploys included an upstream ``base_url`` on each
    entry. The fetch passes the extra field through unchanged — the
    merge layer ignores it. This guards against breakage during the
    server's deploy window when both shapes are live."""

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
    result = fetch_cloud_models("https://api.example.com", "tok-1")
    assert result == [{"id": "gpt-4o", "base_url": "https://upstream.example/v1"}]


def test_fetch_strips_trailing_slash_on_api_url(monkeypatch):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    fetch_cloud_models("https://api.example.com/", "tok-1")
    assert captured["url"] == "https://api.example.com/v1/chat/models"


def test_fetch_non_200_returns_empty(monkeypatch):
    """Server-side gate (e.g. 401, 503) → empty list, don't raise."""
    monkeypatch.setattr(
        "httpx.Client",
        lambda **_: _mock_client(lambda _req: httpx.Response(401, json={"detail": "nope"})),
    )
    assert fetch_cloud_models("https://api.example.com", "bad-token") == []


def test_fetch_network_error_returns_empty(monkeypatch):
    """Connection refused / DNS failure / etc. → degrade silently."""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    assert fetch_cloud_models("https://api.example.com", "tok-1") == []


def test_fetch_unexpected_payload_shape_returns_empty(monkeypatch):
    """Server returns ``{"models": null}`` (or any non-list) → empty."""
    monkeypatch.setattr(
        "httpx.Client",
        lambda **_: _mock_client(lambda _req: httpx.Response(200, json={"models": None})),
    )
    assert fetch_cloud_models("https://api.example.com", "tok-1") == []


def test_fetch_filters_entries_without_id(monkeypatch):
    """Defensive: any entry without an ``id`` field is dropped silently."""

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
    result = fetch_cloud_models("https://api.example.com", "tok-1")
    assert result == [{"id": "gpt-4o"}]


# ── merge_into_registry ──────────────────────────────────────────────


_API = "https://api.example.com"


def test_merge_adds_new_entries():
    registry: dict[str, dict] = {}
    cloud = [{"id": "gpt-4o"}, {"id": "anthropic/claude-opus-4-7"}]
    added = merge_into_registry(registry, cloud, _API)
    assert added == 2
    assert set(registry.keys()) == {"gpt-4o", "anthropic/claude-opus-4-7"}


def test_merge_routes_through_ember_server_even_for_legacy_payload():
    """Older server deploys sent an upstream ``base_url`` alongside
    the ``id``. The merge must ignore it — every cloud entry routes
    through ``{api_url}/v1`` (ember-server's chat proxy) regardless.
    Routing upstream directly with the Ember Cloud JWT always 401s
    and surfaces as a confusing "Unknown model" in the chat UI."""
    registry: dict[str, dict] = {}
    merge_into_registry(
        registry,
        [{"id": "MiniMaxAI/MiniMax-M2.5", "base_url": "https://upstream.example/v1"}],
        _API,
    )
    entry = registry["MiniMaxAI/MiniMax-M2.5"]
    assert entry["url"] == "https://api.example.com/v1"
    # Defensive: the upstream URL must not survive anywhere on the
    # entry — even as a stray field — so a future routing bug
    # can't accidentally pick it up.
    assert "upstream.example" not in str(entry)


def test_merge_entry_shape_matches_local_registry():
    """New entries must be drop-in compatible with the local registry
    shape (``provider``, ``model_id``, ``url``, ``api_key``) so the
    existing ``ModelRegistry.get_model`` resolution path works."""
    registry: dict[str, dict] = {}
    merge_into_registry(registry, [{"id": "gpt-4o"}], _API)
    entry = registry["gpt-4o"]
    assert entry["provider"] == "openai_like"
    assert entry["model_id"] == "gpt-4o"
    # URL always points at ember-server's chat proxy, never upstream.
    assert entry["url"] == "https://api.example.com/v1"
    # ``cloud_token`` is the sentinel that the API-key resolver rewrites
    # to ``CloudCredentials.access_token`` at call time. Critical:
    # nothing else should land here, or the resolver mishandles it.
    assert entry["api_key"] == "cloud_token"
    # Tag so future code (e.g. a "managed by cloud" picker badge) can
    # distinguish cloud-discovered rows from user-defined ones.
    assert entry["source"] == "cloud"


def test_merge_strips_trailing_slash_on_api_url():
    """``api_url`` with a trailing slash → proxy URL still resolves to
    a clean ``{host}/v1`` (no double slash)."""
    registry: dict[str, dict] = {}
    merge_into_registry(registry, [{"id": "gpt-4o"}], "https://api.example.com/")
    assert registry["gpt-4o"]["url"] == "https://api.example.com/v1"


def test_merge_does_not_overwrite_existing():
    """User-defined entries win — same-name cloud entries are skipped."""
    user_entry = {
        "provider": "openai_like",
        "model_id": "gpt-4o",
        "url": "https://my-litellm.example/v1",
        "api_key": "sk-pinned",
        "timeout": 120,
    }
    registry = {"gpt-4o": user_entry}
    added = merge_into_registry(registry, [{"id": "gpt-4o"}], _API)
    assert added == 0
    assert registry["gpt-4o"] is user_entry
    assert registry["gpt-4o"]["url"] == "https://my-litellm.example/v1"
    assert registry["gpt-4o"]["api_key"] == "sk-pinned"


def test_merge_is_idempotent():
    """Re-fetching the same catalogue is a no-op on the second pass."""
    registry: dict[str, dict] = {}
    cloud = [{"id": "gpt-4o"}]
    assert merge_into_registry(registry, cloud, _API) == 1
    assert merge_into_registry(registry, cloud, _API) == 0
    assert len(registry) == 1


def test_merge_skips_entries_without_id():
    """Defensive: ``fetch`` filters these too, but the merge layer
    re-checks so it's safe to call with raw payloads."""
    registry: dict[str, dict] = {}
    added = merge_into_registry(
        registry,
        [{"id": "gpt-4o"}, {"id": ""}, {}],
        _API,
    )
    assert added == 1
    assert set(registry.keys()) == {"gpt-4o"}
