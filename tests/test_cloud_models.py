"""Tests for config/cloud_models.py — fetch + merge of the Ember Cloud
key-pool catalogue.

Covers both pieces independently:

* ``fetch_cloud_models`` — uses ``httpx.MockTransport`` so we don't
  hit a real network. Verifies happy path, no-token short-circuit,
  non-200 / non-JSON / network-error degradation.
* ``merge_into_registry`` — pure in-memory dict mutation. Verifies
  add-new, skip-existing (user config wins), idempotency, and the
  entry shape (``api_key: "cloud_token"`` sentinel + ``source:
  "cloud"`` tag).
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
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "models": [
                    {"id": "gpt-4o", "base_url": "https://api.openai.com/v1"},
                    {"id": "anthropic/claude-opus-4-7", "base_url": "https://openrouter.ai/api/v1"},
                ]
            },
        )

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = fetch_cloud_models("https://api.example.com", "tok-1")

    assert captured["url"] == "https://api.example.com/v1/cli/chat/models"
    assert captured["auth"] == "Bearer tok-1"
    assert result == [
        {"id": "gpt-4o", "base_url": "https://api.openai.com/v1"},
        {"id": "anthropic/claude-opus-4-7", "base_url": "https://openrouter.ai/api/v1"},
    ]


def test_fetch_strips_trailing_slash_on_api_url(monkeypatch):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    fetch_cloud_models("https://api.example.com/", "tok-1")
    assert captured["url"] == "https://api.example.com/v1/cli/chat/models"


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
                    {"id": "gpt-4o", "base_url": "https://api.openai.com/v1"},
                    {"base_url": "https://no-id.example/v1"},  # missing id
                    "not-a-dict",  # noqa: ERA001
                ]
            },
        )

    monkeypatch.setattr("httpx.Client", lambda **_: _mock_client(handler))
    result = fetch_cloud_models("https://api.example.com", "tok-1")
    assert result == [{"id": "gpt-4o", "base_url": "https://api.openai.com/v1"}]


# ── merge_into_registry ──────────────────────────────────────────────


def test_merge_adds_new_entries():
    registry: dict[str, dict] = {}
    cloud = [
        {"id": "gpt-4o", "base_url": "https://api.openai.com/v1"},
        {"id": "anthropic/claude-opus-4-7", "base_url": "https://openrouter.ai/api/v1"},
    ]
    added = merge_into_registry(registry, cloud)
    assert added == 2
    assert set(registry.keys()) == {"gpt-4o", "anthropic/claude-opus-4-7"}


def test_merge_entry_shape_matches_local_registry():
    """New entries must be drop-in compatible with the local registry
    shape (``provider``, ``model_id``, ``url``, ``api_key``) so the
    existing ``ModelRegistry.get_model`` resolution path works."""
    registry: dict[str, dict] = {}
    merge_into_registry(registry, [{"id": "gpt-4o", "base_url": "https://api.openai.com/v1"}])
    entry = registry["gpt-4o"]
    assert entry["provider"] == "openai_like"
    assert entry["model_id"] == "gpt-4o"
    assert entry["url"] == "https://api.openai.com/v1"
    # ``cloud_token`` is the sentinel that the API-key resolver rewrites
    # to ``CloudCredentials.access_token`` at call time. Critical:
    # nothing else should land here, or the resolver mishandles it.
    assert entry["api_key"] == "cloud_token"
    # Tag so future code (e.g. a "managed by cloud" picker badge) can
    # distinguish cloud-discovered rows from user-defined ones.
    assert entry["source"] == "cloud"


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
    added = merge_into_registry(
        registry,
        [{"id": "gpt-4o", "base_url": "https://will-not-be-applied.example/v1"}],
    )
    assert added == 0
    assert registry["gpt-4o"] is user_entry
    assert registry["gpt-4o"]["url"] == "https://my-litellm.example/v1"
    assert registry["gpt-4o"]["api_key"] == "sk-pinned"


def test_merge_is_idempotent():
    """Re-fetching the same catalogue is a no-op on the second pass."""
    registry: dict[str, dict] = {}
    cloud = [{"id": "gpt-4o", "base_url": "https://api.openai.com/v1"}]
    assert merge_into_registry(registry, cloud) == 1
    assert merge_into_registry(registry, cloud) == 0
    assert len(registry) == 1


def test_merge_skips_entries_without_id():
    """Defensive: ``fetch`` filters these too, but the merge layer
    re-checks so it's safe to call with raw payloads."""
    registry: dict[str, dict] = {}
    added = merge_into_registry(
        registry,
        [
            {"id": "gpt-4o", "base_url": "https://api.openai.com/v1"},
            {"id": "", "base_url": "https://empty-id.example/v1"},
            {"base_url": "https://no-id.example/v1"},
        ],
    )
    assert added == 1
    assert set(registry.keys()) == {"gpt-4o"}


def test_merge_handles_missing_base_url():
    """Best-effort: a cloud entry with no ``base_url`` still gets added
    (the user can fix it in config). Ensures we don't crash on the
    missing key."""
    registry: dict[str, dict] = {}
    added = merge_into_registry(registry, [{"id": "lonely-model"}])
    assert added == 1
    assert registry["lonely-model"]["url"] == ""
