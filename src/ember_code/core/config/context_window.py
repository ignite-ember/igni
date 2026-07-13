"""Context window resolution for model providers.

Extracted from :mod:`ember_code.core.config.models` so the model
registry has one job (mapping names → Agno model instances) and
the "how big is this model's context" concern lives on its own.

Resolution order:

1. Explicit ``context_window`` in the registry entry — user
   overrides always win.
2. Dynamic fetch from the provider's OpenAI-compatible
   ``/models/{id}`` endpoint (async only). Cached per model_id.
3. Fallback to :data:`DEFAULT_CONTEXT_WINDOW` (128k) so the caller
   never sees ``None``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 128_000


class ContextWindowResolver:
    """Resolves the context window size for a model.

    Instance state is a single ``dict[str, int]`` cache of
    successful async fetches. Sync ``resolve`` never hits the
    network — it consults the cache + config entry only.
    """

    def __init__(self) -> None:
        self._cache: dict[str, int] = {}

    def resolve(self, model_id: str, entry: dict[str, Any] | None = None) -> int:
        """Return the context window size for *model_id* (synchronous)."""
        if entry and "context_window" in entry:
            return int(entry["context_window"])
        if model_id in self._cache:
            return self._cache[model_id]
        return DEFAULT_CONTEXT_WINDOW

    async def aresolve(self, model_id: str, entry: dict[str, Any] | None = None) -> int:
        """Return the context window size, with async API fallback."""
        if entry and "context_window" in entry:
            return int(entry["context_window"])
        if model_id in self._cache:
            return self._cache[model_id]

        # Try fetching from the provider's /models endpoint
        if entry and "url" in entry:
            fetched = await self._fetch_from_api(
                model_id=model_id,
                base_url=entry["url"],
                api_key=entry.get("api_key") or os.environ.get(entry.get("api_key_env", ""), ""),
            )
            if fetched:
                self._cache[model_id] = fetched
                return fetched

        return DEFAULT_CONTEXT_WINDOW

    async def _fetch_from_api(self, model_id: str, base_url: str, api_key: str = "") -> int | None:
        """Fetch context window from an OpenAI-compatible ``/models/{id}`` endpoint."""
        url = f"{base_url.rstrip('/')}/models/{model_id}"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    for key in ("context_window", "context_length", "max_model_len"):
                        if key in data:
                            return int(data[key])
        except Exception as e:
            logger.debug("Could not fetch context window for %s: %s", model_id, e)
        return None
