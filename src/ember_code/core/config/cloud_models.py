"""Discover models available in the Ember Cloud key pool.

The operator adds AI keys on the portal (per-org overrides or the
global pool); the server exposes the deduplicated ``(model, base_url)``
catalogue at ``GET /v1/cli/chat/models``. We fetch that on session
start and merge each entry into the local registry so ``/model``
surfaces them without the user editing config files.

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
    """Return ``[{id, base_url}, ...]`` from the server, or empty on any failure.

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

    url = f"{api_url.rstrip('/')}/v1/cli/chat/models"
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {cloud_token}"})
        if resp.status_code != 200:
            logger.debug(
                "cloud_models: %s returned %s — skipping merge", url, resp.status_code
            )
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
) -> int:
    """Add cloud-discovered models into ``registry`` in place.

    Returns the number of new entries added. Skips any name that's
    already in the registry — user/project config always wins.
    """
    added = 0
    for entry in cloud_models:
        name = entry.get("id")
        if not name or name in registry:
            continue
        registry[name] = {
            "provider": "openai_like",
            "model_id": name,
            "url": entry.get("base_url") or "",
            # ``api_key: "cloud_token"`` is the existing sentinel the
            # API-key resolver understands — see ``models.py:354-358``.
            # It resolves to the stored CloudCredentials at call time.
            "api_key": "cloud_token",
            "source": "cloud",
        }
        added += 1
    return added
