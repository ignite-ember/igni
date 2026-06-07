"""Discover models available in the Ember Cloud key pool.

The operator adds AI keys on the portal (per-org overrides or the
global pool); the server exposes the catalogue at
``GET /v1/chat/models`` as ``{"models": [{"id": "..."}, ...]}`` —
just the model identifiers. The server intentionally does **not**
return upstream provider URLs: the CLI talks to ember-server's
``/chat/completions`` proxy, which routes to whichever
``(key, base_url)`` pair the pool picks. Leaking the upstream URL
to the client would just expose routing internals.

All cloud-discovered entries are wired to route through
``{api_url}/v1`` on the local side. Older server deploys that
still send a ``base_url`` field are tolerated — the client
deliberately ignores it.

User-defined entries always win — never overwrite an existing key in
``settings.models.registry``. Same-name entries from cloud become
no-ops, which lets users pin a custom config (different timeout,
provider override) without it getting clobbered on the next startup.

Failure modes are all soft:
* No cloud token (user not logged in) → skip silently.
* Network error / timeout / 4xx → log debug and skip; the CLI still
  starts with whatever's in local config.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tight timeout — this runs synchronously on session start, so blocking
# the CLI for 15s on a flaky network would be visibly painful.
_FETCH_TIMEOUT_SECONDS = 3.0


def fetch_cloud_models(api_url: str, cloud_token: str | None) -> list[dict[str, Any]]:
    """Return ``[{id: <model-id>}, ...]`` from the server, or empty on any failure.

    The server response shape is ``{"models": [{"id": "..."}, ...]}``.
    Older deploys may also include ``base_url`` on each entry; the
    extra field is forwarded through this call but ignored downstream
    in :py:func:`merge_into_registry`.

    Synchronous: ``Session.__init__`` is sync and runs at every CLI
    invocation, so we can't add an asyncio dependency here. The hot
    path is cached upstream so the call is cheap.
    """
    if not cloud_token:
        logger.debug("cloud_models: no token, skipping fetch")
        return []
    try:
        import httpx
    except ImportError:
        logger.debug("cloud_models: httpx not installed, skipping fetch")
        return []

    url = f"{api_url.rstrip('/')}/v1/chat/models"
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {cloud_token}"})
        if resp.status_code != 200:
            logger.debug("cloud_models: %s returned %s — skipping merge", url, resp.status_code)
            return []
        payload = resp.json()
    except Exception as exc:
        logger.debug("cloud_models: fetch failed (%s) — skipping merge", exc)
        return []

    models = payload.get("models") or []
    if not isinstance(models, list):
        logger.debug("cloud_models: unexpected payload shape — skipping merge")
        return []
    return [entry for entry in models if isinstance(entry, dict) and entry.get("id")]


def merge_into_registry(
    registry: dict[str, dict[str, Any]],
    cloud_models: list[dict[str, Any]],
    api_url: str,
) -> int:
    """Add cloud-discovered models into ``registry`` in place.

    Every cloud entry is wired to route through ``{api_url}/v1`` —
    the Ember Cloud chat proxy that understands the
    ``cloud_token`` JWT. Only the model ``id`` from the discovery
    response is used; any other field the server might include
    (legacy ``base_url`` from older deploys, etc.) is ignored on
    purpose so the routing never accidentally bypasses the proxy.

    Returns the number of new entries added. Skips any name that's
    already in the registry — user/project config always wins.
    """
    added = 0
    proxy_url = f"{api_url.rstrip('/')}/v1"
    for entry in cloud_models:
        name = entry.get("id")
        if not name or name in registry:
            continue
        registry[name] = {
            "provider": "openai_like",
            "model_id": name,
            # All cloud entries route through ember-server, never
            # the upstream. The ``base_url`` field the server may
            # send back is informational and deliberately unused.
            "url": proxy_url,
            # ``api_key: "cloud_token"`` is the existing sentinel the
            # API-key resolver understands — see ``models.py:354-358``.
            # It resolves to the stored CloudCredentials at call time.
            "api_key": "cloud_token",
            "source": "cloud",
        }
        added += 1
    return added
